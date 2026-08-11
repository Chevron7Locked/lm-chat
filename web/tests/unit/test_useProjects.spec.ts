/**
 * Unit tests for the Projects v1 TanStack Query hooks.
 *
 * Phase-6 review fix (P2 #7): cover the cache-key shapes + the
 * centralized invalidation fan-out so future refactors can't silently
 * break the structural keys the plan v4 §Phase 6 table locks in.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const mockRequest = vi.fn();
const mockPostForm = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args),
    postForm: (...args: unknown[]) => mockPostForm(...args),
  },
  ApiClient: vi.fn(),
}));

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

describe("projectKeys", () => {
  it("all key is ['projects']", async () => {
    const { projectKeys } = await import("@/hooks/useProjects");
    expect(projectKeys.all).toEqual(["projects"]);
  });

  it("detail(id) key includes id", async () => {
    const { projectKeys } = await import("@/hooks/useProjects");
    expect(projectKeys.detail(7)).toEqual(["projects", 7]);
  });

  // Wave 2 (#17) — includeArchived gets its own cache slot.
  it("list() defaults to includeArchived: false", async () => {
    const { projectKeys } = await import("@/hooks/useProjects");
    expect(projectKeys.list()).toEqual([
      "projects",
      { includeArchived: false },
    ]);
  });

  it("list(true) differs from list(false)", async () => {
    const { projectKeys } = await import("@/hooks/useProjects");
    expect(projectKeys.list(true)).not.toEqual(projectKeys.list(false));
  });
});

describe("useProjects", () => {
  it("fetches /api/projects on mount", async () => {
    const { useProjects } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([]);

    const { result } = renderHook(() => useProjects(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockRequest).toHaveBeenCalledWith("/api/projects");
  });

  // Wave 2 (#17) — includeArchived=true adds the query param.
  it("fetches /api/projects?include_archived=true when includeArchived=true", async () => {
    const { useProjects } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([]);

    const { result } = renderHook(() => useProjects(true), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockRequest).toHaveBeenCalledWith(
      "/api/projects?include_archived=true",
    );
  });
});

describe("useArchiveProject / useUnarchiveProject", () => {
  it("POSTs /api/projects/{id}/archive", async () => {
    const { useArchiveProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockPostForm.mockResolvedValue({ id: 7, archived_at: 123 });

    const { result } = renderHook(() => useArchiveProject(), { wrapper });
    const updated = await result.current.mutateAsync({ projectId: 7 });

    expect(updated.archived_at).toBe(123);
    expect(mockPostForm).toHaveBeenCalledWith("/api/projects/7/archive", {});
  });

  it("POSTs /api/projects/{id}/unarchive", async () => {
    const { useUnarchiveProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockPostForm.mockResolvedValue({ id: 7, archived_at: null });

    const { result } = renderHook(() => useUnarchiveProject(), { wrapper });
    const updated = await result.current.mutateAsync({ projectId: 7 });

    expect(updated.archived_at).toBeNull();
    expect(mockPostForm).toHaveBeenCalledWith("/api/projects/7/unarchive", {});
  });
});

describe("useRegenerateProjectSummary", () => {
  it("POSTs /api/projects/{id}/regenerate-summary and invalidates the project detail query", async () => {
    const { useRegenerateProjectSummary, projectKeys } = await import(
      "@/hooks/useProjects"
    );
    const { wrapper, qc } = makeWrapper();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mockPostForm.mockResolvedValue({
      id: 7,
      summary: "A fresh summary.",
      summary_updated_at: 12345,
    });

    const { result } = renderHook(() => useRegenerateProjectSummary(7), {
      wrapper,
    });
    const updated = await result.current.mutateAsync();

    expect(updated.summary).toBe("A fresh summary.");
    expect(mockPostForm).toHaveBeenCalledWith(
      "/api/projects/7/regenerate-summary",
      {},
    );
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: projectKeys.detail(7),
    });
  });
});

describe("project mutations refresh the sidebar chat list (T1-12)", () => {
  it("useCreateProject invalidates chatKeys.all so the sidebar's listDirect refetches", async () => {
    const { useCreateProject } = await import("@/hooks/useProjects");
    const { chatKeys } = await import("@/hooks/useChats");
    const { wrapper, qc } = makeWrapper();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mockPostForm.mockResolvedValue({ id: 11, name: "New Project" });

    const { result } = renderHook(() => useCreateProject(), { wrapper });
    await result.current.mutateAsync({ name: "New Project" });

    // The sidebar renders chatKeys.listDirect (["chats","list-direct",…]).
    // Invalidating the narrow chatKeys.list (["chats","list"]) never matched it
    // (React Query matches keys structurally), so a project create/move/delete
    // looked like a no-op until a window refocus. The fan-out must invalidate
    // chatKeys.all (["chats"]) — the prefix of every chat query, listDirect
    // included.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: chatKeys.all });
    // Guard the exact bug: chatKeys.all must be a prefix of the listDirect key.
    expect(chatKeys.all).toEqual(["chats"]);
    expect(chatKeys.listDirect().slice(0, chatKeys.all.length)).toEqual([
      ...chatKeys.all,
    ]);
  });
});

// fe-10 cleanup: invalidateAllProjectScoped used to ALSO fan out to
// per-project-id scoped keys (["folders",{projectId}], ["documents",
// {projectId}]) and per-chat keys (chatKeys.detail/messages) via an `opts`
// param. Those were redundant no-ops — TanStack's default invalidation
// match is prefix-based, so the four broad-prefix invalidations below
// already cover every scoped/nested query. This guards the cleanup: only
// the four broad calls fire, and the (now-removed) scoped fan-out doesn't
// silently come back.
describe("invalidateAllProjectScoped fan-out (fe-10 cleanup)", () => {
  it("useDeleteProject invalidates exactly the four broad-prefix keys — no per-id scoped fan-out", async () => {
    const { useDeleteProject, projectKeys } = await import(
      "@/hooks/useProjects"
    );
    const { chatKeys } = await import("@/hooks/useChats");
    const { FOLDERS_QUERY_KEY } = await import("@/hooks/useFolders");
    const { wrapper, qc } = makeWrapper();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mockRequest.mockResolvedValue(undefined);

    const { result } = renderHook(() => useDeleteProject(), { wrapper });
    await result.current.mutateAsync({ projectId: 42 });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: projectKeys.all });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: chatKeys.all });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: FOLDERS_QUERY_KEY,
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["documents"] });
    // Exactly four calls total: no redundant per-project-id
    // (["folders"/"documents", {projectId}]) or per-chat-id
    // (chatKeys.detail/messages) fan-out.
    expect(invalidateSpy).toHaveBeenCalledTimes(4);
  });

  it("usePromoteChatToProject invalidates exactly the same four broad-prefix keys — no per-chat-id scoped fan-out", async () => {
    const { usePromoteChatToProject, projectKeys } = await import(
      "@/hooks/useProjects"
    );
    const { chatKeys } = await import("@/hooks/useChats");
    const { FOLDERS_QUERY_KEY } = await import("@/hooks/useFolders");
    const { wrapper, qc } = makeWrapper();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    mockPostForm.mockResolvedValue({ id: 99, moved_document_count: 0 });

    const { result } = renderHook(() => usePromoteChatToProject(5), {
      wrapper,
    });
    await result.current.mutateAsync({});

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: projectKeys.all });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: chatKeys.all });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: FOLDERS_QUERY_KEY,
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["documents"] });
    expect(invalidateSpy).toHaveBeenCalledTimes(4);
  });
});

describe("useProjectKnowledgeStats", () => {
  it("fetches /api/projects/{id}/knowledge-stats", async () => {
    const { useProjectKnowledgeStats } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({
      corpus_tokens: 500,
      threshold: 8000,
      ctx_window: 131000,
    });

    const { result } = renderHook(() => useProjectKnowledgeStats(7), {
      wrapper,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockRequest).toHaveBeenCalledWith(
      "/api/projects/7/knowledge-stats",
    );
    expect(result.current.data?.corpus_tokens).toBe(500);
  });

  it("is disabled (no fetch) when projectId is null", async () => {
    const { useProjectKnowledgeStats } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();

    renderHook(() => useProjectKnowledgeStats(null), { wrapper });

    expect(mockRequest).not.toHaveBeenCalled();
  });
});

describe("useExportProject", () => {
  it("fetches the export bundle and triggers a file download", async () => {
    const { useExportProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    const bundle = {
      exported_at: "2026-07-07T00:00:00Z",
      project: {
        name: "My Project",
        description: "",
        system_prompt: "",
        default_model_id: null,
        rag_threshold: null,
        embedding_model_id: null,
      },
      documents: [],
      chats: [],
    };
    mockRequest.mockResolvedValue(bundle);

    const clickSpy = vi.fn();
    const realCreateElement = document.createElement.bind(document);
    const createElSpy = vi
      .spyOn(document, "createElement")
      .mockImplementation((tag: string) => {
        const el = realCreateElement(tag);
        if (tag === "a") (el as HTMLAnchorElement).click = clickSpy;
        return el;
      });
    const createObjectURLSpy = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:mock-url");
    const revokeObjectURLSpy = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);

    const { result } = renderHook(() => useExportProject(), { wrapper });
    await result.current.mutateAsync({ projectId: 7, name: "My Project" });

    expect(mockRequest).toHaveBeenCalledWith("/api/projects/7/export");
    expect(clickSpy).toHaveBeenCalled();
    expect(createObjectURLSpy).toHaveBeenCalled();
    expect(revokeObjectURLSpy).toHaveBeenCalledWith("blob:mock-url");

    createElSpy.mockRestore();
    createObjectURLSpy.mockRestore();
    revokeObjectURLSpy.mockRestore();
  });
});

describe("useProject", () => {
  it("uses sentinel key when projectId is null (review P2)", async () => {
    const { useProject } = await import("@/hooks/useProjects");
    const { wrapper, qc } = makeWrapper();

    renderHook(() => useProject(null), { wrapper });

    // Sentinel key should be present, NOT projectKeys.detail(0).
    const cache = qc.getQueryCache().getAll();
    const keys = cache.map((q) => q.queryKey);
    expect(keys).toContainEqual(["projects", "noop"]);
    expect(keys).not.toContainEqual(["projects", 0]);
    // Query is disabled — no fetch.
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("fetches /api/projects/:id when id given", async () => {
    const { useProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ id: 42, name: "p" });

    const { result } = renderHook(() => useProject(42), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockRequest).toHaveBeenCalledWith("/api/projects/42");
  });
});

describe("useUpdateProject", () => {
  // PROJECTS-V1 additions Phase 8 (B2) / Phase 10 (D2) remediation — the
  // form-encoding for the two newly-wired nullable fields.
  it("form-encodes default_model_id and rag_threshold", async () => {
    const { useUpdateProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ id: 7, name: "p" });

    const { result } = renderHook(() => useUpdateProject(7), { wrapper });
    await result.current.mutateAsync({
      default_model_id: "qwen3.6-35b-a3b",
      rag_threshold: 4096,
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/projects/7",
      expect.objectContaining({ method: "PATCH" }),
    );
    const callArgs = mockRequest.mock.calls[0];
    const opts = callArgs?.[1] as { body?: string } | undefined;
    const params = new URLSearchParams(opts?.body ?? "");
    expect(params.get("default_model_id")).toBe("qwen3.6-35b-a3b");
    expect(params.get("rag_threshold")).toBe("4096");
  });

  it("form-encodes clear= for both fields together", async () => {
    const { useUpdateProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ id: 7, name: "p" });

    const { result } = renderHook(() => useUpdateProject(7), { wrapper });
    await result.current.mutateAsync({
      clear: "default_model_id,rag_threshold",
    });

    const callArgs = mockRequest.mock.calls[0];
    const opts = callArgs?.[1] as { body?: string } | undefined;
    const params = new URLSearchParams(opts?.body ?? "");
    expect(params.get("clear")).toBe("default_model_id,rag_threshold");
  });
});

describe("useCreateChatInProject", () => {
  it("returns typed ChatSummary (no unsafe cast at call site)", async () => {
    const { useCreateChatInProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockPostForm.mockResolvedValue({ id: 99, title: "x" });

    const { result } = renderHook(() => useCreateChatInProject(7), {
      wrapper,
    });
    const created = await result.current.mutateAsync({ title: "x" });

    expect(created.id).toBe(99);
    expect(mockPostForm).toHaveBeenCalledWith(
      "/api/projects/7/chats",
      expect.objectContaining({ title: "x" }),
    );
  });
});

describe("usePromoteChatToProject", () => {
  it("POSTs /api/chats/{chatId}/promote-to-project with the given fields", async () => {
    const { usePromoteChatToProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockPostForm.mockResolvedValue({
      id: 5,
      user_id: 1,
      name: "Custom",
      description: "",
      system_prompt: "Be terse.",
      created_at: 0,
      updated_at: 0,
      moved_document_count: 2,
    });

    const { result } = renderHook(() => usePromoteChatToProject(7), {
      wrapper,
    });
    const created = await result.current.mutateAsync({
      name: "Custom",
      system_prompt: "Be terse.",
      document_ids: [3, 4],
    });

    expect(created.id).toBe(5);
    expect(created.moved_document_count).toBe(2);
    expect(mockPostForm).toHaveBeenCalledWith(
      "/api/chats/7/promote-to-project",
      { name: "Custom", system_prompt: "Be terse.", document_ids: "3,4" },
    );
  });

  it("omits document_ids when the list is empty", async () => {
    const { usePromoteChatToProject } = await import("@/hooks/useProjects");
    const { wrapper } = makeWrapper();
    mockPostForm.mockResolvedValue({
      id: 6,
      user_id: 1,
      name: "x",
      description: "",
      system_prompt: "",
      created_at: 0,
      updated_at: 0,
      moved_document_count: 0,
    });

    const { result } = renderHook(() => usePromoteChatToProject(7), {
      wrapper,
    });
    await result.current.mutateAsync({ document_ids: [] });

    expect(mockPostForm).toHaveBeenCalledWith(
      "/api/chats/7/promote-to-project",
      {},
    );
  });
});
