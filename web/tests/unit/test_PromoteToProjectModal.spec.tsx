/**
 * Unit tests for PromoteToProjectModal — "Turn this chat into a Project."
 *
 * Pins:
 *  - the form renders the name field (prefilled with chatTitle), the
 *    custom-instructions textarea, and the un-projected document picker
 *    (a document already in a project is filtered out).
 *  - the focused document is pre-checked when it's in the un-projected list.
 *  - submitting POSTs /api/chats/{chatId}/promote-to-project with the
 *    right form payload (name, system_prompt, comma-joined document_ids).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
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
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

const pushMock = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: pushMock }),
}));

const DOCS = [
  {
    id: 1,
    title: "Un-projected A",
    mime_type: "text/plain",
    byte_size: 10,
    chunk_count: 1,
    embedding_model_id: "modelA",
    sha256: "sha-a",
    uploaded_at: "2026-01-01T00:00:00Z",
    project_id: null,
  },
  {
    id: 2,
    title: "Un-projected B",
    mime_type: "text/plain",
    byte_size: 10,
    chunk_count: 1,
    embedding_model_id: "modelA",
    sha256: "sha-b",
    uploaded_at: "2026-01-01T00:00:00Z",
    project_id: null,
  },
  {
    id: 3,
    title: "Already Projected",
    mime_type: "text/plain",
    byte_size: 10,
    chunk_count: 1,
    embedding_model_id: "modelA",
    sha256: "sha-c",
    uploaded_at: "2026-01-01T00:00:00Z",
    project_id: 99,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockRequest.mockResolvedValue(DOCS);
});

function wrap(node: ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PromoteToProjectModal", () => {
  it("renders null when closed", async () => {
    const { PromoteToProjectModal } = await import(
      "@/components/PromoteToProjectModal"
    );
    wrap(
      <PromoteToProjectModal
        open={false}
        onClose={vi.fn()}
        chatId={7}
        chatTitle="My Chat"
      />,
    );
    expect(screen.queryByTestId("promote-to-project-modal")).toBeNull();
  });

  it("renders the name field prefilled, instructions field, and the un-projected doc list", async () => {
    const { PromoteToProjectModal } = await import(
      "@/components/PromoteToProjectModal"
    );
    wrap(
      <PromoteToProjectModal
        open
        onClose={vi.fn()}
        chatId={7}
        chatTitle="My Research Chat"
      />,
    );

    const nameInput = await screen.findByTestId<HTMLInputElement>(
      "promote-to-project-name",
    );
    expect(nameInput.value).toBe("My Research Chat");
    expect(screen.getByTestId("promote-to-project-instructions")).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByTestId("promote-to-project-doc-1")).toBeTruthy();
      expect(screen.getByTestId("promote-to-project-doc-2")).toBeTruthy();
    });
    // Already-projected document (id 3) is filtered out of the picker.
    expect(screen.queryByTestId("promote-to-project-doc-3")).toBeNull();
    expect(screen.getByText("Un-projected A")).toBeTruthy();
    expect(screen.getByText("Un-projected B")).toBeTruthy();
  });

  it("pre-checks the focused document when it's in the un-projected list", async () => {
    const { PromoteToProjectModal } = await import(
      "@/components/PromoteToProjectModal"
    );
    wrap(
      <PromoteToProjectModal
        open
        onClose={vi.fn()}
        chatId={7}
        chatTitle="My Chat"
        focusedDocumentId={2}
      />,
    );

    const checkbox2 = await screen.findByTestId<HTMLInputElement>(
      "promote-to-project-doc-2",
    );
    const checkbox1 = screen.getByTestId<HTMLInputElement>(
      "promote-to-project-doc-1",
    );
    expect(checkbox2.checked).toBe(true);
    expect(checkbox1.checked).toBe(false);
  });

  it("submitting calls postForm with name, system_prompt, and comma-joined document_ids", async () => {
    mockPostForm.mockResolvedValue({
      id: 42,
      user_id: 1,
      name: "Custom Name",
      description: "",
      system_prompt: "Be terse.",
      created_at: 0,
      updated_at: 0,
      moved_document_count: 1,
    });
    const { PromoteToProjectModal } = await import(
      "@/components/PromoteToProjectModal"
    );
    const onClose = vi.fn();
    wrap(
      <PromoteToProjectModal
        open
        onClose={onClose}
        chatId={7}
        chatTitle="My Chat"
      />,
    );

    const nameInput = await screen.findByTestId<HTMLInputElement>(
      "promote-to-project-name",
    );
    fireEvent.change(nameInput, { target: { value: "Custom Name" } });
    fireEvent.change(
      screen.getByTestId("promote-to-project-instructions"),
      { target: { value: "Be terse." } },
    );

    const checkbox1 = await screen.findByTestId<HTMLInputElement>(
      "promote-to-project-doc-1",
    );
    fireEvent.click(checkbox1);

    fireEvent.click(screen.getByTestId("promote-to-project-submit"));

    await waitFor(() => {
      expect(mockPostForm).toHaveBeenCalledWith(
        "/api/chats/7/promote-to-project",
        {
          name: "Custom Name",
          system_prompt: "Be terse.",
          document_ids: "1",
        },
      );
    });
    await waitFor(() => {
      expect(onClose).toHaveBeenCalled();
    });
  });

  it("submitting with no documents selected omits document_ids", async () => {
    mockPostForm.mockResolvedValue({
      id: 43,
      user_id: 1,
      name: "My Chat",
      description: "",
      system_prompt: "",
      created_at: 0,
      updated_at: 0,
      moved_document_count: 0,
    });
    const { PromoteToProjectModal } = await import(
      "@/components/PromoteToProjectModal"
    );
    wrap(
      <PromoteToProjectModal
        open
        onClose={vi.fn()}
        chatId={7}
        chatTitle="My Chat"
      />,
    );

    await screen.findByTestId("promote-to-project-name");
    fireEvent.click(screen.getByTestId("promote-to-project-submit"));

    await waitFor(() => {
      expect(mockPostForm).toHaveBeenCalledWith(
        "/api/chats/7/promote-to-project",
        { name: "My Chat", system_prompt: "" },
      );
    });
  });
});
