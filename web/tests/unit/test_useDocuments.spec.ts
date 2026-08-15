/**
 * Unit tests for useDocuments hooks.
 *
 * Covers: query hooks, mutation hooks, error paths (Invariant 2).
 *
 * Error-path tests verify isError + error.status for every mutation hook
 * per P8b.1 brief §Item 7 (Invariant 2) and P8a item 5 methodology.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// ─── Mock api module ─────────────────────────────────────────────────────────

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args), postForm: vi.fn() },
  ApiClient: vi.fn(),
}));

// ─── Mock authStore ───────────────────────────────────────────────────────────

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

// ─── documentKeys tests ───────────────────────────────────────────────────────

describe("documentKeys", () => {
  it("all is ['documents']", async () => {
    const { documentKeys } = await import("@/hooks/useDocuments");
    expect(documentKeys.all).toEqual(["documents"]);
  });

  it("list() contains 'list'", async () => {
    const { documentKeys } = await import("@/hooks/useDocuments");
    expect(documentKeys.list()).toContain("list");
  });

  it("chunks(5) contains chunk id", async () => {
    const { documentKeys } = await import("@/hooks/useDocuments");
    expect(documentKeys.chunks(5)).toContain(5);
  });
});

// ─── useDocuments tests ───────────────────────────────────────────────────────

describe("useDocuments", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("fetches /api/documents on mount", async () => {
    const { useDocuments } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([]);

    const { result } = renderHook(() => useDocuments(), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(mockRequest).toHaveBeenCalledWith("/api/documents");
  });

  it("returns Document[] directly (no envelope — Invariant 3)", async () => {
    const { useDocuments } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    const doc = {
      id: 1, title: "test.txt", mime_type: "text/plain",
      byte_size: 100, chunk_count: 3, embedding_model_id: "nomic",
      sha256: "abc", uploaded_at: "2026-01-01T00:00:00Z",
    };
    mockRequest.mockResolvedValue([doc]);

    const { result } = renderHook(() => useDocuments(), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(Array.isArray(result.current.data)).toBe(true);
    expect(result.current.data?.[0]?.title).toBe("test.txt");
  });

  it("isError on server failure", async () => {
    const { useDocuments } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    mockRequest.mockRejectedValue(new Error("server error"));

    const { result } = renderHook(() => useDocuments(), { wrapper });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
  });
});

// ─── useUploadDocument tests ──────────────────────────────────────────────────

describe("useUploadDocument", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("POSTs to /api/documents with FormData", async () => {
    const { useUploadDocument } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ id: 1, filename: "test.txt", chunk_count: 3 });

    const { result } = renderHook(() => useUploadDocument(), { wrapper });
    const file = new File(["content"], "test.txt", { type: "text/plain" });

    await act(async () => {
      await result.current.mutateAsync(file);
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/documents",
      expect.objectContaining({ method: "POST" })
    );
    // Body should be FormData.
    const callArgs = mockRequest.mock.calls[0];
    const opts = callArgs?.[1] as { body?: unknown } | undefined;
    expect(opts?.body).toBeInstanceOf(FormData);
  });

  // PROJECTS-V1 additions Phase 8 / #15 remediation — single round-trip
  // upload straight into a project, no un-projected-then-move race window.
  it("POSTs to /api/projects/:id/documents when projectId is given", async () => {
    const { useUploadDocument } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue({ id: 1, filename: "test.txt", chunk_count: 3 });

    const { result } = renderHook(() => useUploadDocument(5), { wrapper });
    const file = new File(["content"], "test.txt", { type: "text/plain" });

    await act(async () => {
      await result.current.mutateAsync(file);
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/projects/5/documents",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("invalidates document list on success", async () => {
    const { useUploadDocument } = await import("@/hooks/useDocuments");
    const { wrapper, qc } = makeWrapper();
    mockRequest.mockResolvedValue({ id: 1, filename: "test.txt", chunk_count: 1 });

    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useUploadDocument(), { wrapper });
    const file = new File(["x"], "test.txt", { type: "text/plain" });

    await act(async () => {
      await result.current.mutateAsync(file);
    });

    expect(invalidateSpy).toHaveBeenCalled();
  });

  // Invariant 2 — error path
  it("propagates isError=true and error.status on 413", async () => {
    const { useUploadDocument } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("file too large"), { status: 413 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useUploadDocument(), { wrapper });
    const file = new File(["x"], "big.txt", { type: "text/plain" });

    act(() => {
      result.current.mutate(file);
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(413);
  });

  // Invariant 2 — error path: unsupported type
  it("propagates isError=true on 415 (unsupported MIME type)", async () => {
    const { useUploadDocument } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("unsupported"), { status: 415 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useUploadDocument(), { wrapper });
    const file = new File(["\x00"], "binary.bin", { type: "application/octet-stream" });

    act(() => {
      result.current.mutate(file);
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(415);
  });
});

// ─── useDeleteDocument tests ──────────────────────────────────────────────────

describe("useDeleteDocument", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("sends DELETE /api/documents/:id", async () => {
    const { useDeleteDocument } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue(undefined);

    const { result } = renderHook(() => useDeleteDocument(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(42);
    });

    expect(mockRequest).toHaveBeenCalledWith(
      "/api/documents/42",
      expect.objectContaining({ method: "DELETE" })
    );
  });

  // Invariant 2 — error path
  it("propagates isError=true and error.status on 404", async () => {
    const { useDeleteDocument } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    const err = Object.assign(new Error("not found"), { status: 404 });
    mockRequest.mockRejectedValue(err);

    const { result } = renderHook(() => useDeleteDocument(), { wrapper });

    act(() => {
      result.current.mutate(999);
    });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect((result.current.error as { status?: number } | null)?.status).toBe(404);
  });
});

// ─── useDocumentChunks tests ──────────────────────────────────────────────────

describe("useDocumentChunks", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("fetches /api/documents/:id/chunks when documentId is non-null", async () => {
    const { useDocumentChunks } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();
    mockRequest.mockResolvedValue([
      { ordinal: 0, text_preview: "First chunk text" },
    ]);

    const { result } = renderHook(() => useDocumentChunks(7), { wrapper });

    await waitFor(() => { expect(result.current.isSuccess).toBe(true); });
    expect(mockRequest).toHaveBeenCalledWith("/api/documents/7/chunks");
  });

  it("is disabled when documentId is null", async () => {
    const { useDocumentChunks } = await import("@/hooks/useDocuments");
    const { wrapper } = makeWrapper();

    const { result } = renderHook(() => useDocumentChunks(null), { wrapper });

    // Should not fire (enabled=false).
    await new Promise((r) => { setTimeout(r, 50); });
    expect(mockRequest).not.toHaveBeenCalled();
    expect(result.current.isSuccess).toBe(false);
  });
});
