/**
 * Unit tests for the sidebar ProjectsSection.
 *
 * Phase-6 review fix (P2 #7). Pins:
 *  - empty/loading/error states with role="status" + retry button
 *  - project rows link to /project/:id
 *  - create form submit calls POST /api/projects
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();
const mockPostForm = vi.fn<(...args: unknown[]) => Promise<unknown>>();

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

// Silence the toast store — we don't need its real impl here.
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: vi.fn() }),
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

describe("ProjectsSection", () => {
  it("renders a row per project with link to /project/:id", async () => {
    const { ProjectsSection } = await import("@/components/ProjectsSection");
    mockRequest.mockResolvedValue([
      {
        id: 3,
        user_id: 1,
        name: "Alpha",
        description: "",
        system_prompt: "",
        folders: [],
        created_at: 0,
        updated_at: 0,
      },
    ]);

    wrap(<ProjectsSection collapsed={false} />);

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-project-row-3")).toBeTruthy();
    });
    const link = screen
      .getByTestId("sidebar-project-row-3")
      .querySelector("a");
    expect(link?.getAttribute("href")).toBe("/project/3");
    expect(screen.getByText("Alpha")).toBeTruthy();
  });

  it("error branch has role=status + retry button (review P2)", async () => {
    const { ProjectsSection } = await import("@/components/ProjectsSection");
    mockRequest.mockRejectedValue(new Error("boom"));

    wrap(<ProjectsSection collapsed={false} />);

    await waitFor(() => {
      expect(screen.getByTestId("sidebar-projects-error")).toBeTruthy();
    });
    const err = screen.getByTestId("sidebar-projects-error");
    expect(err.getAttribute("role")).toBe("status");
    expect(screen.getByTestId("sidebar-projects-retry")).toBeTruthy();
  });

  it("submitting the create form POSTs /api/projects", async () => {
    const { ProjectsSection } = await import("@/components/ProjectsSection");
    mockRequest.mockResolvedValue([]);
    mockPostForm.mockResolvedValue({
      id: 11,
      user_id: 1,
      name: "Newp",
      description: "",
      system_prompt: "",
      folders: [],
      created_at: 0,
      updated_at: 0,
    });

    wrap(<ProjectsSection collapsed={false} />);

    fireEvent.click(screen.getByTestId("sidebar-projects-new-btn"));
    const input = await screen.findByTestId("sidebar-projects-create-input");
    fireEvent.change(input, { target: { value: "Newp" } });
    fireEvent.click(screen.getByTestId("sidebar-projects-create-submit"));

    await waitFor(() => {
      expect(mockPostForm).toHaveBeenCalledWith(
        "/api/projects",
        expect.objectContaining({ name: "Newp" }),
      );
    });
  });
});
