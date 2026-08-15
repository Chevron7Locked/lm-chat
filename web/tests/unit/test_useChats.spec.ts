/**
 * Unit tests for useChats / useMessages / mutation hooks.
 *
 * Strategy: wrap in QueryClientProvider; mock api.request; verify
 * query key shapes, enabled flags, and mutation side-effects.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api module ─────────────────────────────────────────────────────────

const mockRequest = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();
const mockPostForm = vi.fn<(path: string, fields: Record<string, string>) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: [path: string, init?: RequestInit]) => mockRequest(...args),
    postForm: (...args: [path: string, fields: Record<string, string>]) => mockPostForm(...args),
  },
  ApiClient: vi.fn(),
}));

// ─── Mock authStore — return isInitializing=false, user set ──────────────────
// Prevents the enabled guard from blocking queries in unit tests.

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { id: 1, username: "test", is_admin: false },
      isInitializing: false,
      isLoading: false,
      error: null,
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

// ─── Wrapper factory ─────────────────────────────────────────────────────────

// Clear both mocks before each test.
// Individual describe blocks may override via their own beforeEach.
beforeEach(() => {
  vi.clearAllMocks();
});

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    qc,
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children),
  };
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("chatKeys", () => {
  it("all key is ['chats']", async () => {
    const { chatKeys } = await import("@/hooks/useChats");
    expect(chatKeys.all).toEqual(["chats"]);
  });

  it("list() key contains 'list'", async () => {
    const { chatKeys } = await import("@/hooks/useChats");
    expect(chatKeys.list()).toContain("list");
  });

  it("messages(id) key includes id", async () => {
    const { chatKeys } = await import("@/hooks/useChats");
    expect(chatKeys.messages(42)).toContain(42);
    expect(chatKeys.messages(42)).toContain("messages");
  });
});

describe("useChats", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetches /api/chats on mount", async () => {
    const { useChats } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    // Backend returns a plain array (not an envelope).
    mockRequest.mockResolvedValue([]);

    const { result } = renderHook(() => useChats(), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(mockRequest).toHaveBeenCalledWith("/api/chats");
  });

  it("normalises plain array response to {chats, total} envelope", async () => {
    const { useChats } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const chat = { id: 1, title: "Hello", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null };
    // Backend returns plain array — hook must normalise to envelope.
    mockRequest.mockResolvedValue([chat]);

    const { result } = renderHook(() => useChats(), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(result.current.data?.chats).toHaveLength(1);
    expect(result.current.data?.chats[0]?.title).toBe("Hello");
    expect(result.current.data?.total).toBe(1);
  });

  it("isLoading is true initially", async () => {
    const { useChats } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    // Promise that never resolves — keep loading.
    mockRequest.mockReturnValue(new Promise(() => { /* never */ }));

    const { result } = renderHook(() => useChats(), { wrapper });
    expect(result.current.isLoading).toBe(true);
  });

  it("isError is true on rejection", async () => {
    const { useChats } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockRejectedValue(new Error("network error"));

    const { result } = renderHook(() => useChats(), { wrapper });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
  });
});

describe("useMessages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not fetch when chatId is null", async () => {
    const { useMessages } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ messages: [], total: 0 });

    const { result } = renderHook(() => useMessages(null), { wrapper });

    // Wait briefly — no fetch should happen.
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(mockRequest).not.toHaveBeenCalled();
    expect(result.current.isFetching).toBe(false);
  });

  it("fetches /api/chats/:id and extracts messages when chatId is provided", async () => {
    const { useMessages } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    // Backend returns ChatWithMessagesResponse — messages are embedded.
    mockRequest.mockResolvedValue({
      id: 7,
      user_id: 1,
      title: "Test",
      folder: null,
      pinned: false,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      messages: [],
      has_more: false,
    });

    const { result } = renderHook(() => useMessages(7), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(mockRequest).toHaveBeenCalledWith("/api/chats/7");
    expect(result.current.data?.messages).toHaveLength(0);
    expect(result.current.data?.has_more).toBe(false);
  });

  // P12c: pagination metadata
  it("exposes has_more=true when backend signals more pages", async () => {
    const { useMessages } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const msg = { id: 5, chat_id: 7, role: "user" as const, content: "hi", reasoning_content: null, created_at: "2026-01-01T00:00:00Z" };
    mockRequest.mockResolvedValue({
      id: 7, user_id: 1, title: "Test", folder: null, pinned: false,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
      messages: [msg],
      has_more: true,
    });

    const { result } = renderHook(() => useMessages(7), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(result.current.data?.has_more).toBe(true);
    expect(result.current.data?.oldest_id).toBe(5);
  });

  it("sets oldest_id to null when messages array is empty", async () => {
    const { useMessages } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({
      id: 7, user_id: 1, title: "Test", folder: null, pinned: false,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
      messages: [],
      has_more: false,
    });

    const { result } = renderHook(() => useMessages(7), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(result.current.data?.oldest_id).toBeNull();
  });

  it("defaults has_more to false when backend omits the field (pre-P12c shape)", async () => {
    const { useMessages } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    // Legacy shape without has_more — hook must default to false.
    mockRequest.mockResolvedValue({
      id: 7, user_id: 1, title: "T", folder: null, pinned: false,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
      messages: [],
    });

    const { result } = renderHook(() => useMessages(7), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(result.current.data?.has_more).toBe(false);
  });
});

describe("useCreateChat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("posts to /api/chats with form-encoded body (backend uses Form(...))", async () => {
    const { useCreateChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const created = { id: 10, title: "New Chat", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null };
    mockPostForm.mockResolvedValue(created);

    const { result } = renderHook(() => useCreateChat(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ title: "New Chat" });
    });

    // Must use postForm (form-encoded), not request (JSON), to match
    // the backend's Form(...) parameter on POST /api/chats.
    expect(mockPostForm).toHaveBeenCalledWith(
      "/api/chats",
      expect.objectContaining({ title: "New Chat" })
    );
  });

  it("omits undefined optional fields from form body", async () => {
    const { useCreateChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const created = { id: 11, title: "New Chat", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null };
    mockPostForm.mockResolvedValue(created);
    mockRequest.mockResolvedValue(created);

    const { result } = renderHook(() => useCreateChat(), { wrapper });

    // Pass all optional fields — exercises the defined branches.
    await act(async () => {
      await result.current.mutateAsync({ title: "T", folder: "f", model_id: "m" });
    });

    const formArg = mockPostForm.mock.calls[0]?.[1] as Record<string, string>;
    expect(formArg).toMatchObject({ title: "T", folder: "f", model_id: "m" });
  });
});

describe("useDeleteChat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sends DELETE /api/chats/:id", async () => {
    const { useDeleteChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() => useDeleteChat(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(5);
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/chats/5",
      expect.objectContaining({ method: "DELETE" })
    );
  });
});

describe("useUpdateChat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sends PATCH /api/chats/:id with form-encoded body (backend uses Form(...))", async () => {
    const { useUpdateChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const updated = { id: 3, title: "Updated", folder: null, pinned: true, updated_at: "2026-01-01T00:00:00Z", model_id: null };
    mockRequest.mockResolvedValue(updated);

    const { result } = renderHook(() => useUpdateChat(3), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ pinned: true });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/chats/3",
      expect.objectContaining({
        method: "PATCH",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );
  });

  it("includes all defined fields in form body", async () => {
    const { useUpdateChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const updated = { id: 3, title: "T", folder: "f", pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null };
    mockRequest.mockResolvedValue(updated);

    const { result } = renderHook(() => useUpdateChat(3), { wrapper });

    await act(async () => {
      // Pass all optional fields — exercises title/folder/pinned branches.
      await result.current.mutateAsync({ title: "T", folder: "f", pinned: false });
    });

    const callArgs = mockRequest.mock.calls[0];
    if (!callArgs) throw new Error("expected mockRequest to have been called");
    const bodyStr = (callArgs[1] as { body?: string }).body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.get("title")).toBe("T");
    expect(params.get("folder")).toBe("f");
    expect(params.get("pinned")).toBe("false");
  });

  it("JSON-encodes tags in the form body (replaces the whole list)", async () => {
    const { useUpdateChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const updated = { id: 3, title: "T", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null, tags: ["work", "urgent"] };
    mockRequest.mockResolvedValue(updated);

    const { result } = renderHook(() => useUpdateChat(3), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ tags: ["work", "urgent"] });
    });

    const callArgs = mockRequest.mock.calls[0];
    if (!callArgs) throw new Error("expected mockRequest to have been called");
    const bodyStr = (callArgs[1] as { body?: string }).body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.get("tags")).toBe(JSON.stringify(["work", "urgent"]));
  });
});

describe("useArchivedChats", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetches /api/chats with unscoped + include_archived=true", async () => {
    const { useArchivedChats } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([]);

    const { result } = renderHook(() => useArchivedChats(), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(mockRequest).toHaveBeenCalledWith(
      "/api/chats?unscoped=true&include_archived=true",
    );
  });

  it("filters the response to only archived rows", async () => {
    const { useArchivedChats } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const active = { id: 1, title: "Active", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null, archived_at: null };
    const archived = { id: 2, title: "Archived", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null, archived_at: "2026-01-02T00:00:00Z" };
    mockRequest.mockResolvedValue([active, archived]);

    const { result } = renderHook(() => useArchivedChats(), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data?.[0]?.id).toBe(2);
  });
});

describe("useArchiveChat / useUnarchiveChat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("useArchiveChat posts to /api/chats/:id/archive", async () => {
    const { useArchiveChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const archived = { id: 5, title: "T", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null, archived_at: "2026-01-02T00:00:00Z" };
    mockPostForm.mockResolvedValue(archived);

    const { result } = renderHook(() => useArchiveChat(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(5);
    });

    expect(mockPostForm).toHaveBeenCalledWith("/api/chats/5/archive", {});
  });

  it("useUnarchiveChat posts to /api/chats/:id/unarchive", async () => {
    const { useUnarchiveChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const restored = { id: 5, title: "T", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null, archived_at: null };
    mockPostForm.mockResolvedValue(restored);

    const { result } = renderHook(() => useUnarchiveChat(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(5);
    });

    expect(mockPostForm).toHaveBeenCalledWith("/api/chats/5/unarchive", {});
  });
});

describe("useForkChat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sends POST /api/chats/:id/fork with form-encoded body (backend uses Form(...))", async () => {
    const { useForkChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const forked = { id: 11, title: "Forked Chat", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null };
    mockRequest.mockResolvedValue(forked);

    const { result } = renderHook(() => useForkChat(4), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ at_message_id: 99 });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/chats/4/fork",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );
  });

  it("encodes at_message_id in the form body", async () => {
    const { useForkChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const forked = { id: 12, title: "Forked", folder: null, pinned: false, updated_at: "2026-01-01T00:00:00Z", model_id: null };
    mockRequest.mockResolvedValue(forked);

    const { result } = renderHook(() => useForkChat(4), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ at_message_id: 42 });
    });

    const callArgs = mockRequest.mock.calls[0];
    if (!callArgs) throw new Error("expected mockRequest to have been called");
    const bodyStr = (callArgs[1] as { body?: string }).body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.get("at_message_id")).toBe("42");
  });
});

describe("useCompactChat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("sends POST /api/chats/:id/compact with form-encoded body (backend uses Form(...))", async () => {
    const { useCompactChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() => useCompactChat(6), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ target_tokens: 4096 });
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/chats/6/compact",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );
  });

  it("encodes target_tokens in the form body", async () => {
    const { useCompactChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ status: "ok" });

    const { result } = renderHook(() => useCompactChat(6), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ target_tokens: 2048 });
    });

    const callArgs = mockRequest.mock.calls[0];
    if (!callArgs) throw new Error("expected mockRequest to have been called");
    const bodyStr = (callArgs[1] as { body?: string }).body ?? "";
    const params = new URLSearchParams(bodyStr);
    expect(params.get("target_tokens")).toBe("2048");
  });
});

// ─── Error-path tests for mutation hooks (P8a item 5) ────────────────────────

describe("useCreateChat — error path", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("propagates isError=true and error.status on 500", async () => {
    const { useCreateChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("server error"), { status: 500, detail: "synthetic" });
    mockPostForm.mockRejectedValue(err);

    const { result } = renderHook(() => useCreateChat(), { wrapper });

    // .mutate() (not .mutateAsync()) is deliberate: it fires-and-forgets so
    // the rejection surfaces as isError state rather than an unhandled
    // rejection here. `await`ing it was a no-op (mutate() returns void, not
    // a Promise) — the waitFor below is what actually waits for the error.
    act(() => {
      result.current.mutate({ title: "Fail" });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(500);
  });
});

describe("useUpdateChat — error path", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("propagates isError=true on 403", async () => {
    const { useUpdateChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("forbidden"), { status: 403, detail: "forbidden" });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useUpdateChat(1), { wrapper });

    // See the useCreateChat error-path test above: .mutate() is deliberate
    // (fire-and-forget), and `await`ing it was a no-op — waitFor below does
    // the actual waiting.
    act(() => {
      result.current.mutate({ title: "Bad" });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(403);
  });
});

describe("useDeleteChat — error path", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("propagates isError=true on 404", async () => {
    const { useDeleteChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("not found"), { status: 404, detail: "not found" });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useDeleteChat(), { wrapper });

    // See the useCreateChat error-path test above: .mutate() is deliberate
    // (fire-and-forget), and `await`ing it was a no-op — waitFor below does
    // the actual waiting.
    act(() => {
      result.current.mutate(999);
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(404);
  });
});

describe("useForkChat — error path", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("propagates isError=true on 500", async () => {
    const { useForkChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("server error"), { status: 500, detail: "synthetic" });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useForkChat(7), { wrapper });

    // See the useCreateChat error-path test above: .mutate() is deliberate
    // (fire-and-forget), and `await`ing it was a no-op — waitFor below does
    // the actual waiting.
    act(() => {
      result.current.mutate({ at_message_id: 0 });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(500);
  });
});

describe("useCompactChat — error path", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("propagates isError=true on 422", async () => {
    const { useCompactChat } = await import("@/hooks/useChats");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("unprocessable"), { status: 422, detail: "bad input" });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useCompactChat(8), { wrapper });

    // See the useCreateChat error-path test above: .mutate() is deliberate
    // (fire-and-forget), and `await`ing it was a no-op — waitFor below does
    // the actual waiting.
    act(() => {
      result.current.mutate({ target_tokens: -1 });
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(422);
  });
});

// fe-11: useGenerateTitle's optimistic cache patch used to target only
// chatKeys.list() with a ChatSummary[]-shaped updater. That key is a
// {chats, total} ENVELOPE (not an array) and is never read by the sidebar,
// which reads chatKeys.listDirect(scope) instead. The fix fans out over
// every cached key under chatKeys.all and branches on the shape actually
// found there.
describe("useGenerateTitle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function makeChat(id: number, title: string) {
    return {
      id,
      title,
      folder: null,
      pinned: false,
      updated_at: "2026-01-01T00:00:00Z",
      model_id: null,
      display_order: 0,
    };
  }

  it("patches BOTH the listDirect (bare array, sidebar's key) and list (envelope) caches when both are mounted simultaneously", async () => {
    const { useGenerateTitle, chatKeys } = await import("@/hooks/useChats");
    const { wrapper, qc } = makeWrapper();

    // Seed both cache shapes the way useChatsDirect (sidebar) and useChats
    // (elsewhere in the app) populate them for the SAME chat id.
    qc.setQueryData(chatKeys.listDirect(), [makeChat(5, "Untitled")]);
    qc.setQueryData(chatKeys.list(), { chats: [makeChat(5, "Untitled")], total: 1 });
    mockRequest.mockResolvedValue({ title: "Generated Title" });

    const { result } = renderHook(() => useGenerateTitle(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(5);
    });

    const direct = qc.getQueryData<{ id: number; title: string }[]>(
      chatKeys.listDirect(),
    );
    expect(direct?.[0]?.title).toBe("Generated Title");

    const envelope = qc.getQueryData<{
      chats: { id: number; title: string }[];
      total: number;
    }>(chatKeys.list());
    expect(envelope?.chats[0]?.title).toBe("Generated Title");
    expect(envelope?.total).toBe(1);
  });

  it("does not throw and leaves non-list shapes (e.g. a chat detail row) untouched", async () => {
    const { useGenerateTitle, chatKeys } = await import("@/hooks/useChats");
    const { wrapper, qc } = makeWrapper();
    const detail = { id: 9, messages: [] };
    qc.setQueryData(chatKeys.detail(9), detail);
    mockRequest.mockResolvedValue({ title: "X" });

    const { result } = renderHook(() => useGenerateTitle(), { wrapper });

    await act(async () => {
      await expect(result.current.mutateAsync(9)).resolves.toBeDefined();
    });

    expect(qc.getQueryData(chatKeys.detail(9))).toEqual(detail);
  });

  it("only patches the row matching chatId, leaving other rows in the same list untouched", async () => {
    const { useGenerateTitle, chatKeys } = await import("@/hooks/useChats");
    const { wrapper, qc } = makeWrapper();
    qc.setQueryData(chatKeys.listDirect(), [
      makeChat(1, "Chat One"),
      makeChat(2, "Untitled"),
    ]);
    mockRequest.mockResolvedValue({ title: "Chat Two Generated" });

    const { result } = renderHook(() => useGenerateTitle(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(2);
    });

    const direct = qc.getQueryData<{ id: number; title: string }[]>(
      chatKeys.listDirect(),
    );
    expect(direct?.[0]?.title).toBe("Chat One");
    expect(direct?.[1]?.title).toBe("Chat Two Generated");
  });

  it("still invalidates chatKeys.all and chatKeys.detail(chatId) (unchanged behavior)", async () => {
    const { useGenerateTitle, chatKeys } = await import("@/hooks/useChats");
    const { wrapper, qc } = makeWrapper();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mockRequest.mockResolvedValue({ title: "Generated" });

    const { result } = renderHook(() => useGenerateTitle(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(3);
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: chatKeys.all });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: chatKeys.detail(3),
    });
  });
});
