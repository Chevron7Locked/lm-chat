/**
 * RagModeBadge unit tests.
 *
 * PROJECTS-V1 additions Phase 11. Pins:
 *  - badge hidden when chatId is null or query is loading/erroring
 *  - badge renders the right label per mode
 *  - tooltip includes corpus/threshold/focused-doc numbers when present
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args),
    postForm: vi.fn(),
  },
  ApiClient: vi.fn(),
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    user: { id: 1, username: "alice" },
    isInitializing: false,
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

function wrap(node: ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RagModeBadge", () => {
  it("renders nothing when chatId is null", async () => {
    const { RagModeBadge } = await import("@/components/RagModeBadge");
    const { container } = wrap(<RagModeBadge chatId={null} />);
    expect(
      container.querySelector('[data-testid="rag-mode-badge"]'),
    ).toBeNull();
  });

  it("renders HYBRID badge when backend returns hybrid", async () => {
    const { RagModeBadge } = await import("@/components/RagModeBadge");
    mockRequest.mockResolvedValue({
      mode: "hybrid",
      project_corpus_tokens: 20000,
      threshold_tokens: 8000,
      focused_document_id: null,
      embedding_status: "ok",
      embedding_model_pinned: null,
      embedding_model_active: "embed-default",
    });

    wrap(<RagModeBadge chatId={42} />);

    await waitFor(() => {
      expect(
        screen.getByTestId("rag-mode-badge").getAttribute("data-mode"),
      ).toBe("hybrid");
    });
    expect(screen.getByTestId("rag-mode-badge").textContent).toContain(
      "Hybrid",
    );
  });

  it("renders FOCUSED badge with focused-doc info in tooltip", async () => {
    const { RagModeBadge } = await import("@/components/RagModeBadge");
    mockRequest.mockResolvedValue({
      mode: "focused",
      project_corpus_tokens: null,
      threshold_tokens: null,
      focused_document_id: 7,
      embedding_status: "ok",
      embedding_model_pinned: null,
      embedding_model_active: "embed-default",
    });

    wrap(<RagModeBadge chatId={42} />);

    await waitFor(() => {
      expect(
        screen.getByTestId("rag-mode-badge").getAttribute("data-mode"),
      ).toBe("focused");
    });
    const badge = screen.getByTestId("rag-mode-badge");
    expect(badge.textContent).toContain("Focused");
    const tooltip = badge.getAttribute("title") ?? "";
    expect(tooltip).toContain("Focused doc: #7");
  });
});
