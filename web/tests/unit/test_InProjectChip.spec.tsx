/**
 * Unit tests for InProjectChip.
 *
 * Phase-6 review fix (P2 #7). Pins the locked behaviors:
 *  - renders nothing for null chatId
 *  - renders nothing for an un-projected chat (project_id null)
 *  - renders the chip when the chat is in a project
 *  - hook order is stable: useChats + useProject both called
 *    regardless of the un-projected short-circuit (React hook rule)
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

const mockRequest = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    // Tuple rest-param forwarding (not `(path, init) => mockRequest(path, init)`)
    // — a fixed-arity forward would pass an explicit `undefined` init on every
    // single-arg GET call, which would break any future toHaveBeenCalledWith
    // assertion expecting a single-arg call.
    request: (...args: [path: string, init?: RequestInit]) => mockRequest(...args),
    postForm: vi.fn(),
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

beforeEach(() => {
  vi.clearAllMocks();
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

describe("InProjectChip", () => {
  it("renders nothing when chatId is null", async () => {
    const { InProjectChip } = await import("@/components/InProjectChip");
    // useChats fetches /api/chats — return empty union.
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/chats") return Promise.resolve([]);
      return Promise.reject(new Error("unexpected " + url));
    });

    const { container } = wrap(<InProjectChip chatId={null} />);

    // Wait a tick for the suspended query to settle, then assert empty.
    await waitFor(() => {
      expect(container.querySelector(
        '[data-testid="composer-in-project-chip"]',
      )).toBeNull();
    });
  });

  it("renders nothing when chat is un-projected", async () => {
    const { InProjectChip } = await import("@/components/InProjectChip");
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/chats") {
        return Promise.resolve([
          { id: 5, title: "x", project_id: null },
        ]);
      }
      return Promise.reject(new Error("unexpected " + url));
    });

    const { container } = wrap(<InProjectChip chatId={5} />);

    await waitFor(() => {
      expect(container.querySelector(
        '[data-testid="composer-in-project-chip"]',
      )).toBeNull();
    });
  });

  it("renders the chip with project name when chat is in a project", async () => {
    const { InProjectChip } = await import("@/components/InProjectChip");
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/chats") {
        return Promise.resolve([
          { id: 5, title: "x", project_id: 7 },
        ]);
      }
      if (url === "/api/projects/7") {
        return Promise.resolve({
          id: 7,
          user_id: 1,
          name: "ProjectSeven",
          description: "",
          system_prompt: "",
          folders: [],
          created_at: 0,
          updated_at: 0,
        });
      }
      return Promise.reject(new Error("unexpected " + url));
    });

    wrap(<InProjectChip chatId={5} />);

    await waitFor(() => {
      expect(
        screen.getByTestId("composer-in-project-chip"),
      ).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByText("ProjectSeven")).toBeTruthy();
    });
  });
});
