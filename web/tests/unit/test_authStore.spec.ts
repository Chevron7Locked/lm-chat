/**
 * Unit tests for web/src/stores/authStore.ts.
 *
 * The store uses Zustand 5. Vitest resets modules between tests via
 * vi.resetModules() so each test gets a fresh store instance.
 *
 * fetch is mocked per-test via global.fetch = vi.fn().
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Re-import store after each module reset to get a fresh instance.
async function freshStore() {
  const { useAuthStore } = await import("@/stores/authStore");
  return useAuthStore;
}

describe("authStore", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.resetAllMocks();
  });

  it("sets isLoading during login and clears it on success", async () => {
    // Mock a successful login response.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ user_id: 1, expires_at: "2026-06-01T00:00:00Z" }),
        { status: 200 }
      )
    );

    const useAuthStore = await freshStore();
    const store = useAuthStore.getState();

    expect(store.isLoading).toBe(false);
    const loginPromise = store.login("alice", "pass123");

    // isLoading is set synchronously at the start of login.
    expect(useAuthStore.getState().isLoading).toBe(true);

    await loginPromise;

    expect(useAuthStore.getState().isLoading).toBe(false);
    expect(useAuthStore.getState().user).toMatchObject({ id: 1, username: "alice" });
    expect(useAuthStore.getState().error).toBeNull();
  });

  it("captures error.detail on 401 and sets error state", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "invalid credentials" }),
        { status: 401 }
      )
    );

    const useAuthStore = await freshStore();
    const store = useAuthStore.getState();

    await expect(store.login("alice", "wrongpass")).rejects.toBeDefined();

    const state = useAuthStore.getState();
    expect(state.error).toBe("invalid credentials");
    expect(state.isLoading).toBe(false);
    expect(state.user).toBeNull();
  });

  it("sets error state on 401 totp required", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "totp required" }),
        { status: 401 }
      )
    );

    const useAuthStore = await freshStore();

    await expect(useAuthStore.getState().login("alice", "pass")).rejects.toBeDefined();

    expect(useAuthStore.getState().error).toBe("totp required");
  });

  it("includes totp_code in form when provided", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ user_id: 3, expires_at: "2026-06-01T00:00:00Z" }),
        { status: 200 }
      )
    );

    const useAuthStore = await freshStore();
    await useAuthStore.getState().login("alice", "pass", "123456");

    const call = vi.mocked(global.fetch).mock.calls[0];
    const body = (call?.[1] as RequestInit | undefined)?.body as string | undefined;
    expect(body).toContain("totp_code=123456");
  });

  it("does not include totp_code when totpCode is empty string", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ user_id: 3, expires_at: "2026-06-01T00:00:00Z" }),
        { status: 200 }
      )
    );

    const useAuthStore = await freshStore();
    await useAuthStore.getState().login("alice", "pass", "");

    const call = vi.mocked(global.fetch).mock.calls[0];
    const body = (call?.[1] as RequestInit | undefined)?.body as string | undefined;
    expect(body).not.toContain("totp_code");
  });

  it("isInitializing starts true and is cleared to false after login", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ user_id: 4, expires_at: "2026-06-01T00:00:00Z", username: "carol", is_admin: false }),
        { status: 200 }
      )
    );

    const useAuthStore = await freshStore();
    // isInitializing starts true.
    expect(useAuthStore.getState().isInitializing).toBe(true);

    await useAuthStore.getState().login("carol", "pass");
    // After login, isInitializing is false.
    expect(useAuthStore.getState().isInitializing).toBe(false);
  });

  it("refresh calls GET /api/auth/me and populates user on 200", async () => {
    // First call: /api/auth/me (from refresh)
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ user_id: 7, username: "hydrated", is_admin: false }),
        { status: 200 }
      )
    );

    const useAuthStore = await freshStore();
    expect(useAuthStore.getState().isInitializing).toBe(true);
    expect(useAuthStore.getState().user).toBeNull();

    await useAuthStore.getState().refresh();

    const state = useAuthStore.getState();
    expect(state.user).toMatchObject({ id: 7, username: "hydrated", is_admin: false });
    expect(state.isInitializing).toBe(false);
  });

  it("refresh sets user=null and isInitializing=false on 401", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "authentication required" }),
        { status: 401 }
      )
    );

    const useAuthStore = await freshStore();
    await useAuthStore.getState().refresh();

    const state = useAuthStore.getState();
    expect(state.user).toBeNull();
    expect(state.isInitializing).toBe(false);
  });

  it("refresh sets isInitializing=false on network error", async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error("network error"));

    const useAuthStore = await freshStore();
    await useAuthStore.getState().refresh();

    expect(useAuthStore.getState().isInitializing).toBe(false);
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("refresh skips /me call when user is already set", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ user_id: 4, expires_at: "2026-06-01T00:00:00Z", username: "carol", is_admin: false }),
        { status: 200 }
      )
    );

    const useAuthStore = await freshStore();
    await useAuthStore.getState().login("carol", "pass");
    const callsAfterLogin = vi.mocked(global.fetch).mock.calls.length;

    // refresh when user is already set — should skip /me and only clear isInitializing.
    await expect(useAuthStore.getState().refresh()).resolves.toBeUndefined();
    // No additional fetch calls.
    expect(vi.mocked(global.fetch)).toHaveBeenCalledTimes(callsAfterLogin);
    expect(useAuthStore.getState().isInitializing).toBe(false);
  });

  it("clears user on logout", async () => {
    // Set up a logged-in state.
    global.fetch = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ user_id: 2, expires_at: "2026-06-01T00:00:00Z" }),
          { status: 200 }
        )
      )
      .mockResolvedValueOnce(
        // logout response
        new Response(JSON.stringify({ status: "ok" }), { status: 200 })
      );

    const useAuthStore = await freshStore();
    await useAuthStore.getState().login("bob", "pass");
    expect(useAuthStore.getState().user).not.toBeNull();

    await useAuthStore.getState().logout();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("fe-14: clears the entire TanStack query cache on logout so a same-browser user switch never shows the previous user's cached data", async () => {
    global.fetch = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ user_id: 9, expires_at: "2026-06-01T00:00:00Z", username: "dave", is_admin: false }),
          { status: 200 }
        )
      )
      .mockResolvedValueOnce(
        // logout response
        new Response(JSON.stringify({ status: "ok" }), { status: 200 })
      );

    const useAuthStore = await freshStore();
    const { queryClient } = await import("@/lib/queryClient");

    // Seed the cache the way user A's session would have populated it.
    queryClient.setQueryData(["chats", "list-direct"], [{ id: 1, title: "user A's chat" }]);
    queryClient.setQueryData(["projects", "list"], [{ id: 2, name: "user A's project" }]);
    expect(queryClient.getQueryCache().getAll().length).toBeGreaterThan(0);

    await useAuthStore.getState().login("dave", "pass");
    await useAuthStore.getState().logout();

    // Every cached query — not just the SSE localStorage keys — must be gone.
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
    expect(queryClient.getQueryData(["chats", "list-direct"])).toBeUndefined();
    expect(queryClient.getQueryData(["projects", "list"])).toBeUndefined();
  });
});
