/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Documents page unit tests — render + upload + delete + chunk preview.
 *
 * Locked behaviours:
 *   - With an empty document list, the empty-state copy renders.
 *   - Selecting files via the hidden <input type="file"> calls the upload
 *     mutation and pushes a success toast per file.
 *   - Dropping files onto the upload zone calls the upload mutation.
 *   - A failing upload pushes an error toast and does NOT throw.
 *   - Clicking a document row's delete button calls the delete mutation
 *     and pushes an info toast on success.
 *   - Clicking a document row's title expands the row; the chunks query
 *     hook is invoked with the document id.
 *
 * useDocuments / useUploadDocument / useDeleteDocument / useDocumentChunks
 * are mocked at module level so the test doesn't need a TanStack Query
 * provider; the Sidebar (pulled in via AppShell) is mocked because it
 * pulls live data this suite doesn't simulate.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import type { ChunkPreview, Document } from "@/hooks/useDocuments";

// ─── Mock toast store ────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush, dismiss: vi.fn() }),
  useToastStore: { getState: () => ({ push: mockPush, dismiss: vi.fn() }) },
}));

// ─── Mock authStore — signed-in user, hydration done ─────────────────────────

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    user: { id: 1, username: "alice", is_admin: false, totp_enabled: false },
    isInitializing: false,
  }),
}));

// ─── Mock useDocuments hooks (no QueryClientProvider needed) ─────────────────

interface MockUseDocumentsResult {
  data: Document[];
  isLoading: boolean;
  isError: boolean;
}

interface MockUseDocumentChunksResult {
  data: ChunkPreview[];
  isLoading: boolean;
  isError: boolean;
}

const mockUploadMutate = vi.fn();
const mockDeleteMutate = vi.fn();
const mockUseDocuments = vi.fn<() => MockUseDocumentsResult>();
const mockUseDocumentChunks = vi.fn<(id: number) => MockUseDocumentChunksResult>();

vi.mock("@/hooks/useDocuments", () => ({
  useDocuments: () => mockUseDocuments(),
  useUploadDocument: () => ({
    mutateAsync: mockUploadMutate,
    isPending: false,
  }),
  useDeleteDocument: () => ({
    mutateAsync: mockDeleteMutate,
    isPending: false,
  }),
  useDocumentChunks: (id: number) => mockUseDocumentChunks(id),
}));

// ─── Mock Sidebar (AppShell pulls it in; depends on hooks we don't stage) ────

vi.mock("@/components/Sidebar", () => ({
  Sidebar: () =>
    createElement("div", { "data-testid": "mock-sidebar" }, "Sidebar"),
}));

// ─── Mock MoveToProjectMenu (depends on QueryClient via useProjects) ─────────
// DocumentRow now renders this; the test runs without a QueryClientProvider.

vi.mock("@/components/MoveToProjectMenu", () => ({
  MoveToProjectMenu: () =>
    createElement("span", { "data-testid": "mock-move-to-project" }),
}));

vi.mock("@/hooks/useProjects", () => ({
  useMoveDocumentToProject: () => ({ mutate: vi.fn() }),
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const DOC_A: Document = {
  id: 1,
  title: "report.pdf",
  mime_type: "application/pdf",
  byte_size: 12_345,
  chunk_count: 7,
  embedding_model_id: "test-embed",
  sha256: "a".repeat(64),
  uploaded_at: "2026-01-01T00:00:00Z",
};

async function freshDocuments() {
  vi.resetModules();
  const mod = await import("@/pages/Documents");
  return mod.default;
}

function renderDocuments(Page: React.ComponentType) {
  return render(
    <MemoryRouter initialEntries={["/documents"]}>
      <Routes>
        <Route path="/documents" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Documents", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockPush.mockClear();
    mockUploadMutate.mockReset();
    mockDeleteMutate.mockReset();
    mockUseDocuments.mockReset();
    mockUseDocumentChunks.mockReset();
    cleanup();
  });

  it("renders the empty-state copy when the document list is empty", async () => {
    mockUseDocuments.mockReturnValue({ data: [], isLoading: false, isError: false });
    const Page = await freshDocuments();
    renderDocuments(Page);

    expect(screen.getByTestId("documents-empty-state")).toBeTruthy();
    expect(
      screen.getByText(/No documents yet\./i),
    ).toBeTruthy();
    // The page heading still renders.
    expect(screen.getByRole("heading", { name: "Documents" })).toBeTruthy();
  });

  it("uploads files selected via the <input type=file> and toasts on success", async () => {
    mockUseDocuments.mockReturnValue({ data: [], isLoading: false, isError: false });
    mockUploadMutate.mockResolvedValue({
      id: 99,
      filename: "notes.md",
      chunk_count: 3,
    });

    const Page = await freshDocuments();
    const { container } = renderDocuments(Page);

    const fileInput = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement | null;
    expect(fileInput).toBeTruthy();
    if (fileInput === null) return;

    const file = new File(["hello"], "notes.md", { type: "text/markdown" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(mockUploadMutate).toHaveBeenCalledTimes(1);
    });
    const arg = mockUploadMutate.mock.calls[0]?.[0] as File;
    expect(arg.name).toBe("notes.md");

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "success",
          message: expect.stringContaining("notes.md"),
        }),
      );
    });
  });

  it("uploads files dropped onto the upload zone", async () => {
    mockUseDocuments.mockReturnValue({ data: [], isLoading: false, isError: false });
    mockUploadMutate.mockResolvedValue({
      id: 100,
      filename: "dropped.pdf",
      chunk_count: 5,
    });

    const Page = await freshDocuments();
    renderDocuments(Page);

    const zone = screen.getByRole("button", {
      name: /Drop files here or click to upload/i,
    });

    const file = new File(["pdf-bytes"], "dropped.pdf", { type: "application/pdf" });
    fireEvent.drop(zone, {
      dataTransfer: {
        files: [file],
        items: [],
        types: ["Files"],
      },
    });

    await waitFor(() => {
      expect(mockUploadMutate).toHaveBeenCalledTimes(1);
    });
    expect((mockUploadMutate.mock.calls[0]?.[0] as File).name).toBe("dropped.pdf");
  });

  it("surfaces an error toast when upload fails", async () => {
    mockUseDocuments.mockReturnValue({ data: [], isLoading: false, isError: false });
    mockUploadMutate.mockRejectedValue(new Error("upload broke"));

    const Page = await freshDocuments();
    const { container } = renderDocuments(Page);

    const fileInput = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement | null;
    if (fileInput === null) throw new Error("file input not rendered");

    const file = new File(["x"], "bad.pdf", { type: "application/pdf" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "error",
          message: expect.stringContaining("bad.pdf"),
        }),
      );
    });
  });

  it("deletes a document and toasts on success", async () => {
    mockUseDocuments.mockReturnValue({
      data: [DOC_A],
      isLoading: false,
      isError: false,
    });
    mockDeleteMutate.mockResolvedValue(undefined);

    const Page = await freshDocuments();
    renderDocuments(Page);

    const deleteBtn = screen.getByRole("button", { name: `Delete ${DOC_A.title}` });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(mockDeleteMutate).toHaveBeenCalledWith(DOC_A.id);
    });
    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "info",
          message: "Document deleted.",
        }),
      );
    });
  });

  it("expands a document row and queries chunk previews", async () => {
    mockUseDocuments.mockReturnValue({
      data: [DOC_A],
      isLoading: false,
      isError: false,
    });
    // Initial (collapsed) render: chunks query is gated by documentId === null;
    // once expanded, the hook is called with DOC_A.id.
    mockUseDocumentChunks.mockReturnValue({
      data: [{ ordinal: 1, text_preview: "first chunk text" }],
      isLoading: false,
      isError: false,
    });

    const Page = await freshDocuments();
    const { container } = renderDocuments(Page);

    // The row-expand button is the .rr-doc-title-btn (the delete button
    // also surfaces DOC_A.title via aria-label, so role+name is ambiguous).
    const titleBtn = container.querySelector(
      "button.rr-doc-title-btn",
    ) as HTMLButtonElement | null;
    expect(titleBtn).toBeTruthy();
    if (titleBtn === null) return;
    expect(titleBtn.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(titleBtn);

    await waitFor(() => {
      const after = container.querySelector(
        "button.rr-doc-title-btn",
      ) as HTMLButtonElement | null;
      expect(after?.getAttribute("aria-expanded")).toBe("true");
    });
    expect(screen.getByText("first chunk text")).toBeTruthy();
    // Chunks hook was queried with the document id.
    expect(mockUseDocumentChunks).toHaveBeenCalledWith(DOC_A.id);
  });
});
