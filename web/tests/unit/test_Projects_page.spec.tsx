/**
 * Unit tests for the Projects all-projects landing page (Wave 2 #12).
 *
 * Pins: active projects render in the main list, archived projects render
 * inside a collapsed "Archived" section, chat/doc counts are derived from
 * the existing unscoped useChats()/useDocuments() lists, and "New project"
 * reuses the existing create-project flow.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { createElement, type ReactNode } from "react";

const mockRequest = vi.fn();
const mockPostForm = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args) as Promise<unknown>,
    postForm: (...args: unknown[]) => mockPostForm(...args) as Promise<unknown>,
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

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

vi.mock("@/components/AppShell", () => ({
  AppShell: ({ children }: { children: ReactNode }) =>
    createElement("div", { "data-testid": "mock-app-shell" }, children),
}));

vi.mock("@/hooks/useDocumentTitle", () => ({
  useDocumentTitle: vi.fn(),
}));

const ACTIVE_PROJECT = {
  id: 1,
  user_id: 1,
  name: "Active One",
  description: "An active project",
  system_prompt: "",
  created_at: 0,
  updated_at: 0,
  archived_at: null,
};

const ARCHIVED_PROJECT = {
  id: 2,
  user_id: 1,
  name: "Old Project",
  description: "",
  system_prompt: "",
  created_at: 0,
  updated_at: 0,
  archived_at: 123,
};

beforeEach(() => {
  vi.clearAllMocks();
  mockRequest.mockImplementation((url: string) => {
    if (url === "/api/projects?include_archived=true") {
      return Promise.resolve([ACTIVE_PROJECT, ARCHIVED_PROJECT]);
    }
    if (url === "/api/chats") {
      return Promise.resolve([
        { id: 10, title: "chat one", project_id: 1 },
        { id: 11, title: "chat two", project_id: 1 },
      ]);
    }
    if (url === "/api/documents") {
      return Promise.resolve([
        {
          id: 100,
          user_id: 1,
          title: "doc.txt",
          mime_type: "text/plain",
          byte_size: 10,
          chunk_count: 1,
          embedding_model_id: "m",
          sha256: "x",
          uploaded_at: "2026-01-01",
          project_id: 1,
        },
      ]);
    }
    return Promise.resolve([]);
  });
});

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return import("@/pages/Projects").then(({ default: Projects }) =>
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/projects"]}>
          <Routes>
            <Route path="/projects" element={<Projects />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  );
}

describe("Projects landing page", () => {
  it("renders active projects in the main list with chat/doc counts", async () => {
    await renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("projects-list")).toBeTruthy();
    });
    expect(screen.getByText("Active One")).toBeTruthy();
    expect(screen.getByText("An active project")).toBeTruthy();
    // 2 chats + 1 doc derived from the unscoped lists above.
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.getByText("1")).toBeTruthy();
  });

  it("does not show the archived project in the main list", async () => {
    await renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("projects-list")).toBeTruthy();
    });
    const mainList = screen.getByTestId("projects-list");
    expect(
      Array.from(mainList.querySelectorAll("li")).some((li) =>
        li.textContent.includes("Old Project"),
      ),
    ).toBe(false);
  });

  it("shows archived projects inside the collapsed Archived section", async () => {
    await renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("projects-archived-section")).toBeTruthy();
    });
    expect(screen.getByText("Archived (1)")).toBeTruthy();
    expect(screen.getByTestId("projects-archived-list").textContent).toContain(
      "Old Project",
    );
  });

  it("each project card links to /project/:id", async () => {
    await renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("projects-card-link-1")).toBeTruthy();
    });
    expect(
      screen.getByTestId("projects-card-link-1").getAttribute("href"),
    ).toBe("/project/1");
  });

  it("New project reveals a form; submitting creates + navigates", async () => {
    mockPostForm.mockResolvedValue({ ...ACTIVE_PROJECT, id: 99, name: "Fresh" });
    await renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("projects-new-trigger")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("projects-new-trigger"));

    await waitFor(() => {
      expect(screen.getByTestId("projects-new-input")).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("projects-new-input"), {
      target: { value: "Fresh" },
    });
    fireEvent.click(screen.getByTestId("projects-new-submit"));

    await waitFor(() => {
      expect(mockPostForm).toHaveBeenCalledWith(
        "/api/projects",
        expect.objectContaining({ name: "Fresh" }),
      );
    });
  });

  it("renders the empty state when there are no projects at all", async () => {
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/projects?include_archived=true") {
        return Promise.resolve([]);
      }
      return Promise.resolve([]);
    });

    await renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("projects-empty")).toBeTruthy();
    });
  });
});
