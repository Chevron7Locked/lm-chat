/**
 * Unit tests for the Project page.
 *
 * Phase-6 punch-list D. Pins the three-tab IA + presets-seeds-prompt
 * behavior + edit-in-place name. The tab strip tracks the URL hash;
 * the presets dropdown copies preset text into the instructions
 * field (spec §"Interaction with the prompt library").
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { createElement, type ReactNode } from "react";

const mockRequest = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();
const mockPostForm = vi.fn<(path: string, fields: Record<string, string>) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: [path: string, init?: RequestInit]) => mockRequest(...args),
    postForm: (...args: [path: string, fields: Record<string, string>]) => mockPostForm(...args),
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

// Stub AppShell — pulls in Sidebar which has deep deps not relevant here.
vi.mock("@/components/AppShell", () => ({
  AppShell: ({ children }: { children: ReactNode }) =>
    createElement("div", { "data-testid": "mock-app-shell" }, children),
}));

vi.mock("@/hooks/useDocumentTitle", () => ({
  useDocumentTitle: vi.fn(),
}));

const PROJECT_FIXTURE = {
  id: 42,
  user_id: 1,
  name: "TestProject",
  description: "A test project",
  system_prompt: "You are helpful.",
  folders: [],
  created_at: 0,
  updated_at: 0,
};

beforeEach(() => {
  vi.clearAllMocks();
  mockRequest.mockImplementation((url: string) => {
    if (url === "/api/projects/42") {
      return Promise.resolve(PROJECT_FIXTURE);
    }
    if (url === "/api/chats?project_id=42") {
      return Promise.resolve([]);
    }
    if (url === "/api/documents") {
      return Promise.resolve([]);
    }
    if (url === "/api/prompts") {
      return Promise.resolve([
        {
          id: 7,
          user_id: 1,
          name: "TechWriter",
          content: "Be precise and brief.",
          created_at: "2026-01-01",
          updated_at: "2026-01-01",
        },
      ]);
    }
    return Promise.resolve([]);
  });
});

function renderAt(path: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return import("@/pages/Project").then(({ default: Project }) =>
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/project/:id" element={<Project />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  );
}

describe("Project page", () => {
  it("renders the project name + description by default on the chats tab", async () => {
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-name")).toBeTruthy();
    });
    expect(screen.getByText("TestProject")).toBeTruthy();
    expect(screen.getByTestId("project-description")).toBeTruthy();
    // Default tab is "chats" — chats list rendered.
    expect(screen.getByTestId("project-chats-list")).toBeTruthy();
  });

  it("clicking the name button opens the rename input", async () => {
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByText("TestProject")).toBeTruthy();
    });
    fireEvent.click(screen.getByLabelText("Rename TestProject"));
    // The input is autoFocus'd so screen.getByDisplayValue works.
    expect(screen.getByDisplayValue("TestProject")).toBeTruthy();
  });

  it("clicking the Documents tab switches the panel", async () => {
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-tab-documents")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-documents"));

    await waitFor(() => {
      expect(screen.getByTestId("project-docs-list")).toBeTruthy();
    });
    expect(screen.getByTestId("project-doc-upload-input")).toBeTruthy();
  });

  it("clicking the Settings tab reveals the presets dropdown", async () => {
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(screen.getByTestId("project-settings-preset")).toBeTruthy();
    });
    expect(screen.getByTestId("project-settings-save")).toBeTruthy();
  });

  it("selecting a preset copies its content into the instructions field", async () => {
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(screen.getByTestId("project-settings-preset")).toBeTruthy();
    });
    // Wait for prompts data to populate (option list).
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "TechWriter" }),
      ).toBeTruthy();
    });

    const prompt = screen.getByTestId(
      "project-settings-prompt",
    ) as HTMLTextAreaElement;
    // Initial value is the project's system_prompt.
    expect(prompt.value).toBe("You are helpful.");

    fireEvent.change(screen.getByTestId("project-settings-preset"), {
      target: { value: "7" },
    });

    await waitFor(() => {
      expect(
        (screen.getByTestId("project-settings-prompt") as HTMLTextAreaElement)
          .value,
      ).toBe("Be precise and brief.");
    });
  });

  // PROJECTS-V1 additions Phase 8 (B2) / Phase 10 (D2) remediation (#13) —
  // the default-model picker + RAG threshold control on the Settings tab.
  it("Settings tab renders the default-model picker and RAG threshold input", async () => {
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(
        screen.getByTestId("project-settings-default-model"),
      ).toBeTruthy();
    });
    expect(screen.getByTestId("project-settings-rag-threshold")).toBeTruthy();
    // No pin on the fixture project → picker starts on the sentinel. The
    // model list itself resolves async (useModels), so wait for it rather
    // than asserting synchronously right after the testid appears.
    await waitFor(() => {
      expect(screen.getByText("Use global default")).toBeTruthy();
    });
  });

  it("selecting a model and saving PATCHes default_model_id", async () => {
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/projects/42") return Promise.resolve(PROJECT_FIXTURE);
      if (url === "/api/chats?project_id=42") return Promise.resolve([]);
      if (url === "/api/documents") return Promise.resolve([]);
      if (url === "/api/models") {
        return Promise.resolve([
          {
            key: "qwen3.6-35b-a3b",
            display_name: "Qwen 3.6 35B A3B",
            capabilities: {},
            loaded_instances: 1,
          },
        ]);
      }
      return Promise.resolve([]);
    });

    await renderAt("/project/42");
    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: /Qwen 3\.6 35B A3B/ }),
      ).toBeTruthy();
    });

    // Rendered option VALUES are composite "<provider>::<model_id>" —
    // same format the chat header `<select>` uses (bug fix: this picker
    // used to render bare ids, which never matched a project's already-set
    // default_model_id). No explicit `provider` on the wire fixture →
    // useModels.ts's normalizer defaults it to "lmstudio".
    fireEvent.change(screen.getByTestId("project-settings-default-model"), {
      target: { value: "lmstudio::qwen3.6-35b-a3b" },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => {
      const patchCall = mockRequest.mock.calls.find(
        (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
      );
      expect(patchCall).toBeTruthy();
    });
    const patchCall = mockRequest.mock.calls.find(
      (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
    );
    const body = (patchCall?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(body);
    // The composite id is decoded back to bare before it's persisted —
    // `default_model_id` has no companion provider column, and
    // create_chat_in_project seeds it verbatim into the new chat's bare
    // chats.model_id, so storage must stay bare.
    expect(params.get("default_model_id")).toBe("qwen3.6-35b-a3b");
  });

  // Bug fix (live review, post-ship): a project whose default_model_id was
  // ALREADY a real, currently-known bare model id used to render as "Use
  // global default" (the picker's rendered option values were bare, so a
  // stored bare id never matched a composite-valued option) — and hitting
  // Save in that state silently CLEARED the pin. This is the round-trip
  // regression test for that fix.
  it("a project with an existing default_model_id shows it selected, and Save preserves it", async () => {
    const projectWithDefault = {
      ...PROJECT_FIXTURE,
      default_model_id: "oym-qimi-122b-a10b-k2.6-i1",
    };
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/projects/42") return Promise.resolve(projectWithDefault);
      if (url === "/api/chats?project_id=42") return Promise.resolve([]);
      if (url === "/api/documents") return Promise.resolve([]);
      if (url === "/api/models") {
        return Promise.resolve([
          {
            key: "oym-qimi-122b-a10b-k2.6-i1",
            display_name: "Oym Qimi 122B A10B K2.6",
            capabilities: {},
            loaded_instances: 1,
          },
        ]);
      }
      return Promise.resolve([]);
    });

    await renderAt("/project/42");
    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      const select = screen.getByTestId(
        "project-settings-default-model",
      ) as HTMLSelectElement;
      // Must resolve to the REAL model's composite value, not the
      // "Use global default" sentinel ("").
      expect(select.value).toBe("lmstudio::oym-qimi-122b-a10b-k2.6-i1");
    });

    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => {
      const patchCall = mockRequest.mock.calls.find(
        (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
      );
      expect(patchCall).toBeTruthy();
    });
    const patchCall = mockRequest.mock.calls.find(
      (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
    );
    const body = (patchCall?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(body);
    // Save without touching the picker must PRESERVE the pin, not clear it.
    expect(params.get("default_model_id")).toBe("oym-qimi-122b-a10b-k2.6-i1");
    expect(params.get("clear")?.split(",") ?? []).not.toContain(
      "default_model_id",
    );
  });

  it("entering a RAG threshold and saving PATCHes rag_threshold", async () => {
    await renderAt("/project/42");
    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(
        screen.getByTestId("project-settings-rag-threshold"),
      ).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("project-settings-rag-threshold"), {
      target: { value: "4096" },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => {
      const patchCall = mockRequest.mock.calls.find(
        (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
      );
      expect(patchCall).toBeTruthy();
    });
    const patchCall = mockRequest.mock.calls.find(
      (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
    );
    const body = (patchCall?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(body);
    expect(params.get("rag_threshold")).toBe("4096");
  });

  it("saving with both fields left blank sends clear= for both", async () => {
    await renderAt("/project/42");
    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(screen.getByTestId("project-settings-save")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => {
      const patchCall = mockRequest.mock.calls.find(
        (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
      );
      expect(patchCall).toBeTruthy();
    });
    const patchCall = mockRequest.mock.calls.find(
      (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
    );
    const body = (patchCall?.[1] as { body?: string } | undefined)?.body ?? "";
    const params = new URLSearchParams(body);
    expect(params.get("clear")).toBe("default_model_id,rag_threshold");
  });

  it("rejects a negative RAG threshold without sending PATCH", async () => {
    await renderAt("/project/42");
    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(
        screen.getByTestId("project-settings-rag-threshold"),
      ).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("project-settings-rag-threshold"), {
      target: { value: "-5" },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    // Let any pending microtasks flush, then confirm no PATCH fired —
    // client-side validation blocked the submit.
    await new Promise((r) => {
      setTimeout(r, 20);
    });
    const patchCall = mockRequest.mock.calls.find(
      (c) => (c[1] as { method?: string } | undefined)?.method === "PATCH",
    );
    expect(patchCall).toBeUndefined();
  });

  it("delete button shows confirm UI before firing", async () => {
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(screen.getByTestId("project-delete-trigger")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-delete-trigger"));
    expect(screen.getByTestId("project-delete-confirm")).toBeTruthy();
  });

  // Wave 2 (#17) — project archiving.
  it("archive button POSTs /api/projects/{id}/archive", async () => {
    mockPostForm.mockResolvedValue({ ...PROJECT_FIXTURE, archived_at: 123 });
    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(screen.getByTestId("project-archive-trigger")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-archive-trigger"));

    await waitFor(() => {
      expect(mockPostForm).toHaveBeenCalledWith(
        "/api/projects/42/archive",
        {},
      );
    });
  });

  it("an archived project shows the Archived badge + an unarchive trigger", async () => {
    const archivedFixture = { ...PROJECT_FIXTURE, archived_at: 123 };
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/projects/42") return Promise.resolve(archivedFixture);
      if (url === "/api/chats?project_id=42") return Promise.resolve([]);
      if (url === "/api/documents") return Promise.resolve([]);
      return Promise.resolve([]);
    });
    mockPostForm.mockResolvedValue({ ...PROJECT_FIXTURE, archived_at: null });

    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-archived-badge")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(screen.getByTestId("project-unarchive-trigger")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-unarchive-trigger"));

    await waitFor(() => {
      expect(mockPostForm).toHaveBeenCalledWith(
        "/api/projects/42/unarchive",
        {},
      );
    });
  });

  // Wave 2 (#16) — project export.
  it("export button fetches the bundle and triggers a download", async () => {
    const bundle = {
      exported_at: "2026-07-07T00:00:00Z",
      project: {
        name: "TestProject",
        description: "A test project",
        system_prompt: "You are helpful.",
        default_model_id: null,
        rag_threshold: null,
        embedding_model_id: null,
      },
      documents: [],
      chats: [],
    };
    mockRequest.mockImplementation((url: string) => {
      if (url === "/api/projects/42") return Promise.resolve(PROJECT_FIXTURE);
      if (url === "/api/chats?project_id=42") return Promise.resolve([]);
      if (url === "/api/documents") return Promise.resolve([]);
      if (url === "/api/projects/42/export") return Promise.resolve(bundle);
      return Promise.resolve([]);
    });

    const clickSpy = vi.fn();
    // Snapshot the REAL implementation before spyOn replaces document.createElement
    // below — calling `document.createElement` from inside realCreateElement would
    // recurse into the spy. Reflect.get (rather than a bare `document.createElement`
    // reference) also sidesteps the deprecated-overload flag on the merged
    // createElement declaration (only the DeprecatedTagNameMap overload is actually
    // deprecated; a bare reference can't select a specific overload, a call can).
    const nativeCreateElement = Reflect.get(document, "createElement");
    const realCreateElement = (tag: string): HTMLElement =>
      nativeCreateElement.call(document, tag) as HTMLElement;
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

    await renderAt("/project/42");

    await waitFor(() => {
      expect(screen.getByTestId("project-tab-settings")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-tab-settings"));

    await waitFor(() => {
      expect(screen.getByTestId("project-export-trigger")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("project-export-trigger"));

    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith("/api/projects/42/export");
    });
    expect(clickSpy).toHaveBeenCalled();

    createElSpy.mockRestore();
    createObjectURLSpy.mockRestore();
    revokeObjectURLSpy.mockRestore();
  });

  // Wave 2 (#14) — KB capacity meter.
  describe("KB capacity meter", () => {
    it("shows the empty state when the project has no documents", async () => {
      await renderAt("/project/42");

      await waitFor(() => {
        expect(screen.getByTestId("project-tab-documents")).toBeTruthy();
      });
      fireEvent.click(screen.getByTestId("project-tab-documents"));

      await waitFor(() => {
        expect(screen.getByTestId("project-kb-meter-empty")).toBeTruthy();
      });
      expect(screen.queryByTestId("project-kb-meter")).toBeNull();
    });

    it("renders the token count + percentage once the project has documents", async () => {
      mockRequest.mockImplementation((url: string) => {
        if (url === "/api/projects/42") {
          return Promise.resolve(PROJECT_FIXTURE);
        }
        if (url === "/api/chats?project_id=42") return Promise.resolve([]);
        if (url === "/api/documents") {
          return Promise.resolve([
            {
              id: 1,
              user_id: 1,
              title: "notes.txt",
              mime_type: "text/plain",
              byte_size: 10,
              chunk_count: 1,
              embedding_model_id: "m",
              sha256: "x",
              uploaded_at: "2026-01-01",
              project_id: 42,
            },
          ]);
        }
        if (url === "/api/projects/42/knowledge-stats") {
          return Promise.resolve({
            corpus_tokens: 500,
            threshold: 1000,
            ctx_window: 131000,
          });
        }
        return Promise.resolve([]);
      });

      await renderAt("/project/42");

      await waitFor(() => {
        expect(screen.getByTestId("project-tab-documents")).toBeTruthy();
      });
      fireEvent.click(screen.getByTestId("project-tab-documents"));

      await waitFor(() => {
        expect(screen.getByTestId("project-kb-meter")).toBeTruthy();
      });
      expect(
        screen.getByText(/500 tokens of knowledge · 50% of the inline threshold/),
      ).toBeTruthy();
    });
  });

  // Wave 3 (#10) — rolling project auto-summary.
  describe("Project summary card", () => {
    it("shows the empty state when the project has no summary yet", async () => {
      await renderAt("/project/42");

      await waitFor(() => {
        expect(screen.getByTestId("project-summary-card")).toBeTruthy();
      });
      expect(screen.getByTestId("project-summary-empty")).toBeTruthy();
      expect(screen.queryByTestId("project-summary-text")).toBeNull();
    });

    it("renders the summary text + relative updated-at once generated", async () => {
      const summarized = {
        ...PROJECT_FIXTURE,
        summary: "The team is researching dark energy.",
        summary_updated_at: Date.now() / 1000,
      };
      mockRequest.mockImplementation((url: string) => {
        if (url === "/api/projects/42") return Promise.resolve(summarized);
        if (url === "/api/chats?project_id=42") return Promise.resolve([]);
        if (url === "/api/documents") return Promise.resolve([]);
        return Promise.resolve([]);
      });

      await renderAt("/project/42");

      await waitFor(() => {
        expect(screen.getByTestId("project-summary-text")).toBeTruthy();
      });
      expect(
        screen.getByText("The team is researching dark energy."),
      ).toBeTruthy();
      expect(screen.getByTestId("project-summary-updated-at")).toBeTruthy();
      expect(screen.queryByTestId("project-summary-empty")).toBeNull();
    });

    it("Regenerate button POSTs /api/projects/{id}/regenerate-summary", async () => {
      mockPostForm.mockResolvedValue({
        ...PROJECT_FIXTURE,
        summary: "A regenerated summary.",
        summary_updated_at: Date.now() / 1000,
      });

      await renderAt("/project/42");

      await waitFor(() => {
        expect(screen.getByTestId("project-summary-regenerate")).toBeTruthy();
      });
      fireEvent.click(screen.getByTestId("project-summary-regenerate"));

      await waitFor(() => {
        expect(mockPostForm).toHaveBeenCalledWith(
          "/api/projects/42/regenerate-summary",
          {},
        );
      });
    });
  });
});
