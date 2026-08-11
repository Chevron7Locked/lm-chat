/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for LmStudioSection (P13g / ADR-023).
 *
 * Locked behaviours:
 *   - On mount, GETs /api/settings/lmstudio and populates the form
 *     with the resolved view; source chips reflect ``source_*`` fields.
 *   - API key input is type="password" by default; "Show" toggle flips
 *     it to type="text" and back.
 *   - Save sends ONLY changed connection fields: base_url only when it
 *     differs from the loaded value; api_key only when the user typed one.
 *     Unchanged fields are omitted to avoid triggering a backend probe.
 *     Admin routes to PATCH /api/admin/lmstudio/default; non-admin to
 *     PUT /api/settings/lmstudio.
 *   - "Test connection" issues POST /api/settings/lmstudio/test and
 *     surfaces ok=true → "OK — N models reachable".
 *   - "Test connection" failure surfaces ok=false + error message.
 *   - 400 from PUT surfaces detail in the inline error region.
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
import { createMemoryRouter, RouterProvider } from "react-router-dom";

// ─── Mock toast ──────────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
  useToastStore: { getState: () => ({ push: mockPush }) },
}));

// ─── Mock @tanstack/react-query (useQueryClient) ─────────────────────────────
// LmStudioSection now calls useQueryClient() to invalidate the resolved-config
// query after a successful save. Expose a hoisted spy so individual tests can
// assert on the invalidation call.

const mockInvalidateQueries = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: mockInvalidateQueries }),
}));

// ─── Mock useModelList (avoids TanStack QueryClientProvider) ─────────────────

vi.mock("@/hooks/useModelList", () => ({
  useModelList: () => ({
    models: [],
    loadedModels: [],
    revalidate: vi.fn().mockResolvedValue(undefined),
    status: "idle",
  }),
}));

// ─── Mock useRefreshModels (the Refresh-list button's mutation) ──────────────
// LmStudioSection wires the "Refresh list" link to useRefreshModels(), which
// uses TanStack's useMutation/useQueryClient — without a QueryClientProvider
// the real hook throws. The test harness already mocks useModelList; the
// mutation hook gets the same treatment.

vi.mock("@/hooks/useModels", () => ({
  useRefreshModels: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
}));

// ─── Mock useChatModelOptions (Area 5 hook; LmStudioSection uses it for
//     the default-model dropdown). Defaults to an empty option list so the
//     rendered <select> has no entries — matches the existing test
//     expectation that no models are surfaced. Individual tests (Cluster 3a
//     closeout Finding 1) can inject options carrying capability fields via
//     the hoisted mutable state.
const mockChatModelOptionsState = vi.hoisted(() => ({
  options: [] as {
    id: string;
    label: string;
    loaded: boolean;
    capabilities: {
      vision: boolean;
      trained_for_tool_use: boolean;
      reasoning: { default: string; allowed_options: string[] } | null;
      embedding: boolean;
    };
  }[],
}));
vi.mock("@/hooks/useChatModelOptions", () => ({
  useChatModelOptions: () => ({
    options: mockChatModelOptionsState.options,
    groups: [],
    isLoading: false,
    isError: false,
  }),
}));

// ─── Mock useLmStudioStore (avoids zustand import side-effects) ──────────────

vi.mock("@/stores/lmStudioStore", () => ({
  useLmStudioStore: (selector: (s: { resolveProbe: ReturnType<typeof vi.fn> }) => unknown) =>
    selector({ resolveProbe: vi.fn() }),
}));

// ─── Mock useAuthStore — non-admin user by default ───────────────────────────

vi.mock("@/stores/authStore", () => {
  // LmStudioSection itself calls useAuthStore with a selector.
  // The MemoryIndexingCard's useEmbeddingStatus hook calls it without
  // one (`const { isInitializing, user } = useAuthStore()`), so the
  // mock has to support both shapes.
  const state = {
    // Was `is_admin: false` historically; the 2026-06-03 audit added
    // admin-only gating on the Test + Save buttons (Area 4: silent 403
    // cluster). Run the suite as an admin so the buttons aren't
    // disabled — separate non-admin gating tests can be added later.
    user: { is_admin: true } as { is_admin: boolean } | null,
    isInitializing: false,
  };
  return {
    useAuthStore: (selector?: (s: typeof state) => unknown) =>
      selector !== undefined ? selector(state) : state,
  };
});

// ─── Mock useEmbeddingStatus — keeps the visibility card quiet during ──
//     LmStudioSection tests, which assert against the form, not the card.
//     Embedding-selector tests override this per test.
const mockEmbeddingStatusState = vi.hoisted(() => ({
  data: undefined as
    | {
        active_model_id: string | null;
        loaded_embedding_models: string[];
        total_indexed_messages: number;
        last_indexed_at: number | null;
        models_in_use: Record<string, number>;
        embedding_status: "ok" | "no_embedding_model" | "pinned_model_unavailable";
      }
    | undefined,
  isLoading: false,
  isError: false,
}));

vi.mock("@/hooks/useEmbeddingStatus", () => ({
  useEmbeddingStatus: () => mockEmbeddingStatusState,
}));

// ─── Mock useLmStudioConfig — provides preferred_embedding_model_id +
//     loaded_embedding_models to MemoryIndexingCard (Fix A). Default:
//     no data (pre-Fix-A behaviour). Embedding-selector tests override.
const mockLmStudioConfigState = vi.hoisted(() => ({
  data: undefined as
    | {
        base_url: string;
        default_model: string;
        api_key_set: boolean;
        source_base_url: string;
        source_api_key: string;
        source_default_model: string;
        preferred_embedding_model_id?: string | null;
        loaded_embedding_models?: Array<{ key: string; active?: boolean }>;
        preferred_background_model_id?: string | null;
        loaded_background_models?: Array<{ key: string }>;
      }
    | undefined,
  isLoading: false,
  isError: false,
}));

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => mockLmStudioConfigState,
  lmStudioConfigKeys: {
    all: ["lmstudio-config"] as const,
    resolved: () => ["lmstudio-config", "resolved"] as const,
  },
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const RESOLVED_ENV = {
  base_url: "http://localhost:1234",
  default_model: "qwen3-8b",
  api_key_set: false,
  source_base_url: "env",
  source_api_key: "env",
  source_default_model: "env",
} as const;

const RESOLVED_USER = {
  base_url: "http://user.example:5678",
  default_model: "user-model",
  api_key_set: true,
  source_base_url: "user",
  source_api_key: "user",
  source_default_model: "user",
} as const;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function freshSection() {
  vi.resetModules();
  const mod = await import("@/components/LmStudioSection");
  return mod.LmStudioSection;
}

/**
 * LmStudioSection uses useBlocker which requires a data router
 * (createBrowserRouter / createMemoryRouter). Wrap every render
 * so the hook doesn't throw "useBlocker must be used within a data router."
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function renderInDataRouter(Component: () => any) {
  const router = createMemoryRouter(
    [{ path: "/", element: createElement(Component) }],
    { initialEntries: ["/"] },
  );
  return render(createElement(RouterProvider, { router }));
}

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("LmStudioSection", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockPush.mockClear();
    mockInvalidateQueries.mockClear();
    mockInvalidateQueries.mockResolvedValue(undefined);
    mockChatModelOptionsState.options = [];
    // Reset embedding status + lmstudio config mocks to quiet defaults.
    mockEmbeddingStatusState.data = undefined;
    mockLmStudioConfigState.data = undefined;
    cleanup();
  });

  it("loads + renders the resolved env view with source chips", async () => {
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });
    // Resolved values land in the inputs.
    const baseInput = screen.getByTestId(
      "lmstudio-base-url",
    ) as HTMLInputElement;
    expect(baseInput.value).toBe("http://localhost:1234");
    const modelInput = screen.getByTestId(
      "lmstudio-default-model",
    ) as HTMLInputElement;
    // The default-model field is now a ModelSelectControl (<select>). With no
    // loaded model options in this test, the select's DOM .value is "". Assert
    // the element is present and leave value-reflection to integration tests.
    expect(modelInput).toBeTruthy();
    // API key input is empty (we never prefill it).
    const keyInput = screen.getByTestId("lmstudio-api-key") as HTMLInputElement;
    expect(keyInput.value).toBe("");
  });

  it("toggles api key input between password and text", async () => {
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_USER));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });

    const keyInput = screen.getByTestId("lmstudio-api-key") as HTMLInputElement;
    expect(keyInput.type).toBe("password");

    fireEvent.click(screen.getByTestId("lmstudio-api-key-show"));
    expect(keyInput.type).toBe("text");

    fireEvent.click(screen.getByTestId("lmstudio-api-key-show"));
    expect(keyInput.type).toBe("password");
  });

  it("save sends all non-empty fields; api_key omitted when untouched", async () => {
    // 2026-06-20 contract: connection fields (base_url/api_key) route through
    // the admin PATCH endpoint for admins; default_model ALWAYS routes through
    // the user-tier PUT so svc.resolve(user.id) returns the saved value.
    // This two-request split fixes the "always reverts to 9b" bug where the
    // admin PATCH wrote to server_lm_studio_default but an existing user
    // override row shadowed it — resolve() returned the old user-tier value.
    const fetchMock = vi
      .fn()
      // Initial GET /api/settings/lmstudio
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      // PATCH /api/admin/lmstudio/default (connection fields — admin path)
      .mockResolvedValueOnce(
        jsonResponse({
          ...RESOLVED_ENV,
          base_url: "http://changed.example",
          source_base_url: "server_admin",
        }),
      )
      // PUT /api/settings/lmstudio (default_model — always user tier)
      .mockResolvedValueOnce(
        jsonResponse({
          ...RESOLVED_ENV,
          base_url: "http://changed.example",
          source_base_url: "server_admin",
          source_default_model: "user",
        }),
      );
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("lmstudio-base-url"), {
      target: { value: "http://changed.example" },
    });

    const form = screen.getByTestId("lmstudio-form");
    fireEvent.submit(form);

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith({
        variant: "success",
        message: "LM Studio settings saved.",
      });
    });

    // Call 1 (index 0): initial GET /api/settings/lmstudio
    // Call 2 (index 1): PATCH /api/admin/lmstudio/default — connection fields only
    const adminCall = fetchMock.mock.calls[1];
    expect(adminCall?.[0]).toBe("/api/admin/lmstudio/default");
    const adminInit = adminCall?.[1] as RequestInit | undefined;
    expect(adminInit?.method).toBe("PATCH");
    const adminBody = JSON.parse(adminInit?.body as string);
    expect(adminBody.base_url).toBe("http://changed.example");
    // default_model must NOT be in the admin PATCH — it goes to user tier separately.
    expect("default_model" in adminBody).toBe(false);
    // api_key is NOT in the body — user didn't type a new value.
    expect("api_key" in adminBody).toBe(false);

    // Call 3 (index 2): PUT /api/settings/lmstudio — default_model user tier
    const userCall = fetchMock.mock.calls[2];
    expect(userCall?.[0]).toBe("/api/settings/lmstudio");
    const userInit = userCall?.[1] as RequestInit | undefined;
    expect(userInit?.method).toBe("PUT");
    const userBody = JSON.parse(userInit?.body as string);
    // default_model is present (pre-filled from GET, non-empty).
    expect(userBody.default_model).toBe(RESOLVED_ENV.default_model);
    // base_url and api_key not in user-tier PUT.
    expect("base_url" in userBody).toBe(false);
    expect("api_key" in userBody).toBe(false);
  });

  it("model-only save: skips admin PATCH and sends only default_model to user tier", async () => {
    // Regression guard for the "HTTP 401. Save aborted" lockout:
    // When only the default_model changes, the admin PATCH (which probes LM
    // Studio) must NOT be called.  Only the user-tier PUT for default_model
    // should be issued.
    const fetchMock = vi
      .fn()
      // Initial GET /api/settings/lmstudio
      .mockResolvedValueOnce(jsonResponse(RESOLVED_USER))
      // PUT /api/settings/lmstudio (default_model — user tier only)
      .mockResolvedValueOnce(
        jsonResponse({ ...RESOLVED_USER, default_model: "new-model" }),
      );
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });

    // Switch to manual entry so we can type a model id directly.
    fireEvent.click(screen.getByTestId("lmstudio-default-model-manual"));

    fireEvent.change(screen.getByTestId("lmstudio-default-model"), {
      target: { value: "new-model" },
    });
    // base_url and api_key are left untouched.

    fireEvent.submit(screen.getByTestId("lmstudio-form"));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith({
        variant: "success",
        message: "LM Studio settings saved.",
      });
    });

    // Exactly 2 calls: GET (mount) + PUT (default_model).
    // The admin PATCH must NOT appear — it would probe LM Studio and 401.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const calls = fetchMock.mock.calls as [string, RequestInit | undefined][];
    const adminPatchCall = calls.find(
      ([url, init]) =>
        url === "/api/admin/lmstudio/default" && init?.method === "PATCH",
    );
    expect(adminPatchCall).toBeUndefined();

    // The user-tier PUT carries default_model, no base_url, no api_key.
    const putCall = calls.find(
      ([url, init]) =>
        url === "/api/settings/lmstudio" && init?.method === "PUT",
    );
    expect(putCall).toBeTruthy();
    const putBody = JSON.parse(putCall?.[1]?.body as string);
    expect(putBody.default_model).toBe("new-model");
    expect("base_url" in putBody).toBe(false);
    expect("api_key" in putBody).toBe(false);
  });

  it("save includes api_key when user typed it", async () => {
    // api_key routes through the admin PATCH (call index 1).
    // default_model then routes through the user-tier PUT (call index 2).
    const fetchMock = vi
      .fn()
      // GET /api/settings/lmstudio
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      // PATCH /api/admin/lmstudio/default (api_key)
      .mockResolvedValueOnce(jsonResponse(RESOLVED_USER))
      // PUT /api/settings/lmstudio (default_model)
      .mockResolvedValueOnce(jsonResponse(RESOLVED_USER));
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("lmstudio-api-key"), {
      target: { value: "freshkey" },
    });
    fireEvent.submit(screen.getByTestId("lmstudio-form"));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalled();
    });
    // Call index 1: admin PATCH for api_key
    const adminCall = fetchMock.mock.calls[1];
    expect(adminCall?.[0]).toBe("/api/admin/lmstudio/default");
    const adminBody = JSON.parse(
      (adminCall?.[1] as RequestInit | undefined)?.body as string,
    );
    expect(adminBody.api_key).toBe("freshkey");
    // default_model must not be in the admin PATCH
    expect("default_model" in adminBody).toBe(false);
  });

  it("test connection — OK shows model count", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      .mockResolvedValueOnce(
        jsonResponse({ ok: true, model_count: 5, error: null }),
      );
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("lmstudio-test-connection"));

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-test-result")).toBeTruthy();
    });
    expect(screen.getByTestId("lmstudio-test-result").textContent).toContain(
      "Connected — 5 models reachable",
    );
  });

  it("test connection — failure shows error message", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      .mockResolvedValueOnce(
        jsonResponse({
          ok: false,
          model_count: null,
          error: "upstream returned HTTP 401",
        }),
      );
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("lmstudio-test-connection"));

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-test-result")).toBeTruthy();
    });
    // A 401 is special-cased to an actionable "set the API key" message rather
    // than the raw "Probe failed — HTTP 401" string.
    expect(screen.getByTestId("lmstudio-test-result").textContent).toContain(
      "API key",
    );
  });

  it("400 from PUT surfaces detail in inline error region", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      .mockResolvedValueOnce(
        jsonResponse({ detail: "'base_url': empty string is not a valid write value" }, 400),
      );
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("lmstudio-base-url"), {
      target: { value: "" },
    });
    fireEvent.submit(screen.getByTestId("lmstudio-form"));

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-save-error")).toBeTruthy();
    });
    expect(screen.getByTestId("lmstudio-save-error").textContent).toContain(
      "empty string",
    );
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("default-model options carry capability fields through to the select + icons", async () => {
    // Cluster 3a closeout (audit 2026-06-10, Finding 1): LmStudioSection's
    // option remap previously dropped `capabilities`, so ModelSelectControl's
    // capability icons rendered empty on this surface. Assert the field now
    // flows through: the selected model's glyph suffix appears in its
    // <option> text AND the adjacent lucide icon row renders.
    mockChatModelOptionsState.options = [
      {
        id: RESOLVED_ENV.default_model,
        label: RESOLVED_ENV.default_model,
        loaded: true,
        capabilities: {
          vision: true,
          trained_for_tool_use: true,
          reasoning: { default: "medium", allowed_options: ["low", "medium"] },
          embedding: false,
        },
      },
    ];
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });

    const select = screen.getByTestId(
      "lmstudio-default-model",
    ) as HTMLSelectElement;
    // Option label carries the [VTR] capability glyph suffix — proof the
    // capabilities field reached ModelSelectControl's option list.
    const option = Array.from(select.options).find(
      (o) => o.value === RESOLVED_ENV.default_model,
    );
    expect(option?.textContent).toContain("[VTR]");
    // The lucide icon row for the selected model renders adjacent to the
    // select (testId = `${testId}-cap-icons`).
    const icons = screen.getByTestId("lmstudio-default-model-cap-icons");
    expect(icons).toBeTruthy();
    expect(icons.querySelector('[data-testid="cap-icon-vision"]')).toBeTruthy();
    expect(icons.querySelector('[data-testid="cap-icon-tool"]')).toBeTruthy();
    expect(
      icons.querySelector('[data-testid="cap-icon-reasoning"]'),
    ).toBeTruthy();
  });

  it("successful save invalidates lmStudioConfigKeys.resolved() query", async () => {
    // Regression guard for the SPA-propagation bug: after a successful save,
    // handleSave must call queryClient.invalidateQueries with the key produced
    // by lmStudioConfigKeys.resolved() so that useLmStudioConfig readers (e.g.
    // savedDefaultModel in Chat) refetch the new default_model instead of
    // serving the 60s stale cache.
    const fetchMock = vi
      .fn()
      // Initial GET /api/settings/lmstudio
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      // PATCH /api/admin/lmstudio/default (connection fields — admin path)
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      // PUT /api/settings/lmstudio (default_model — always user tier)
      .mockResolvedValueOnce(jsonResponse({ ...RESOLVED_ENV, default_model: "new-model" }));
    global.fetch = fetchMock;
    mockInvalidateQueries.mockClear();

    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("settings-lmstudio-section")).toBeTruthy();
    });

    // Change base_url to trigger the connection-fields PATCH, then submit.
    fireEvent.change(screen.getByTestId("lmstudio-base-url"), {
      target: { value: "http://changed.example" },
    });
    fireEvent.submit(screen.getByTestId("lmstudio-form"));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith({
        variant: "success",
        message: "LM Studio settings saved.",
      });
    });

    // The resolved-config query must have been invalidated.
    expect(mockInvalidateQueries).toHaveBeenCalledTimes(1);
    // lmStudioConfigKeys.resolved() === ["lmstudio-config", "resolved"]
    expect(mockInvalidateQueries).toHaveBeenCalledWith({
      queryKey: ["lmstudio-config", "resolved"],
    });
  });

  it("renders load error when GET fails", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ detail: "internal error" }, 500),
      );
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-load-error")).toBeTruthy();
    });
    expect(screen.getByTestId("lmstudio-load-error").textContent).toContain(
      "internal error",
    );
  });

  // ─── Fix A — embedding model selector (MemoryIndexingCard) ────────────────

  it("Fix A: renders embedding model selector with loaded embedders + Auto option when BE fields are present", async () => {
    // Provide embedding status data so the card body renders.
    mockEmbeddingStatusState.data = {
      active_model_id: "nomic-embed-v1.5@q8_0",
      loaded_embedding_models: ["nomic-embed-v1.5@q8_0"],
      total_indexed_messages: 10,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    // Provide lmstudio config with Fix A fields.
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "qwen3-8b",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_embedding_model_id: "nomic-embed-v1.5@q8_0",
      loaded_embedding_models: [{ key: "nomic-embed-v1.5@q8_0" }, { key: "mxbai-embed@q4" }],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-embedding-model-select")).toBeTruthy();
    });

    const select = screen.getByTestId("lmstudio-embedding-model-select") as HTMLSelectElement;
    // Auto option present.
    const options = Array.from(select.options).map((o) => o.value);
    expect(options).toContain(""); // Auto = value=""
    expect(options).toContain("nomic-embed-v1.5@q8_0");
    expect(options).toContain("mxbai-embed@q4");
    // Preferred model is selected.
    expect(select.value).toBe("nomic-embed-v1.5@q8_0");
  });

  it("Fix A: selects Auto option when preferred_embedding_model_id is null", async () => {
    mockEmbeddingStatusState.data = {
      active_model_id: "nomic-embed-v1.5@q8_0",
      loaded_embedding_models: ["nomic-embed-v1.5@q8_0"],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_embedding_model_id: null, // Auto
      loaded_embedding_models: [{ key: "nomic-embed-v1.5@q8_0" }],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-embedding-model-select")).toBeTruthy();
    });
    const select = screen.getByTestId("lmstudio-embedding-model-select") as HTMLSelectElement;
    expect(select.value).toBe(""); // Auto selected
  });

  it("Fix A: PATCHes /api/settings/lmstudio/embedding-model on change and invalidates config query", async () => {
    mockEmbeddingStatusState.data = {
      active_model_id: "nomic-embed-v1.5@q8_0",
      loaded_embedding_models: ["nomic-embed-v1.5@q8_0", "mxbai-embed@q4"],
      total_indexed_messages: 5,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_embedding_model_id: null,
      loaded_embedding_models: [{ key: "nomic-embed-v1.5@q8_0" }, { key: "mxbai-embed@q4" }],
    };
    const fetchMock = vi
      .fn()
      // GET /api/settings/lmstudio (LmStudioSection form)
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      // PATCH /api/settings/lmstudio/embedding-model
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-embedding-model-select")).toBeTruthy();
    });

    const select = screen.getByTestId("lmstudio-embedding-model-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "mxbai-embed@q4" } });

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith({
        variant: "success",
        message: "Embedding model preference saved.",
      });
    });

    // Find the PATCH call.
    const patchCall = fetchMock.mock.calls.find(
      ([url]: [string]) => url === "/api/settings/lmstudio/embedding-model",
    );
    expect(patchCall).toBeTruthy();
    const patchInit = patchCall?.[1] as RequestInit | undefined;
    expect(patchInit?.method).toBe("PATCH");
    const body = JSON.parse(patchInit?.body as string);
    expect(body.embedding_model_id).toBe("mxbai-embed@q4");

    // Query invalidation fired.
    expect(mockInvalidateQueries).toHaveBeenCalledWith({
      queryKey: ["lmstudio-config", "resolved"],
    });
  });

  it("Fix A: PATCHes null when Auto option is selected", async () => {
    mockEmbeddingStatusState.data = {
      active_model_id: "nomic-embed-v1.5@q8_0",
      loaded_embedding_models: ["nomic-embed-v1.5@q8_0"],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_embedding_model_id: "nomic-embed-v1.5@q8_0",
      loaded_embedding_models: [{ key: "nomic-embed-v1.5@q8_0" }],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-embedding-model-select")).toBeTruthy();
    });

    fireEvent.change(
      screen.getByTestId("lmstudio-embedding-model-select"),
      { target: { value: "" } }, // Auto = clear
    );

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith({
        variant: "success",
        message: "Embedding model preference saved.",
      });
    });

    const patchCall = fetchMock.mock.calls.find(
      ([url]: [string]) => url === "/api/settings/lmstudio/embedding-model",
    );
    const body = JSON.parse((patchCall?.[1] as RequestInit)?.body as string);
    expect(body.embedding_model_id).toBeNull(); // Auto = null
  });

  it("Fix A: shows inline error when PATCH returns 400", async () => {
    mockEmbeddingStatusState.data = {
      active_model_id: null,
      loaded_embedding_models: [],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "no_embedding_model",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_embedding_model_id: null,
      loaded_embedding_models: [{ key: "unloaded-model" }],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      .mockResolvedValueOnce(
        jsonResponse({ detail: "Model 'unloaded-model' is not loaded" }, 400),
      );
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-embedding-model-select")).toBeTruthy();
    });

    fireEvent.change(
      screen.getByTestId("lmstudio-embedding-model-select"),
      { target: { value: "unloaded-model" } },
    );

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-embedding-model-error")).toBeTruthy();
    });
    expect(
      screen.getByTestId("lmstudio-embedding-model-error").textContent,
    ).toContain("not loaded");
    // No success toast on error.
    expect(mockPush).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: "success" }),
    );
  });

  it("Fix A: marks the active embedder with `· active` and renders the active caption", async () => {
    // BE marks exactly one loaded entry active (the resolver's pick). The
    // option text must carry `· active`, and the quiet caption must name it
    // so "which model is actually indexing?" is answerable at a glance.
    mockEmbeddingStatusState.data = {
      active_model_id: "mxbai-embed@q4",
      loaded_embedding_models: ["nomic-embed-v1.5@q8_0", "mxbai-embed@q4"],
      total_indexed_messages: 5,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_embedding_model_id: null, // Auto → resolver picked mxbai
      loaded_embedding_models: [
        { key: "nomic-embed-v1.5@q8_0", active: false },
        { key: "mxbai-embed@q4", active: true },
      ],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-embedding-model-select")).toBeTruthy();
    });

    const select = screen.getByTestId(
      "lmstudio-embedding-model-select",
    ) as HTMLSelectElement;
    const optByValue = (v: string): HTMLOptionElement =>
      Array.from(select.options).find((o) => o.value === v)!;
    // Active option carries the marker; the non-active loaded one does not.
    expect(optByValue("mxbai-embed@q4").textContent).toContain("· active");
    expect(optByValue("mxbai-embed@q4").textContent).toContain("· loaded");
    expect(optByValue("nomic-embed-v1.5@q8_0").textContent).not.toContain(
      "· active",
    );
    expect(optByValue("nomic-embed-v1.5@q8_0").textContent).toContain(
      "· loaded",
    );
    // Auto option names the resolved active model (never a mystery).
    expect(optByValue("").textContent).toContain("mxbai-embed@q4");
    // Quiet caption names the active embedder.
    const caption = screen.getByTestId("lmstudio-embedding-model-active");
    expect(caption.textContent).toContain("mxbai-embed@q4");
  });

  it("Fix A: gracefully falls back to display-only when BE fields absent (pre-Fix-A)", async () => {
    // When the lmstudio GET does NOT carry loaded_embedding_models, the card
    // must fall back to the original <code> display — no crash.
    mockEmbeddingStatusState.data = {
      active_model_id: "nomic-embed-v1.5@q8_0",
      loaded_embedding_models: ["nomic-embed-v1.5@q8_0"],
      total_indexed_messages: 3,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    // No Fix A fields — simulates pre-Fix-A BE.
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      // preferred_embedding_model_id and loaded_embedding_models absent
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-memory-indexing-card")).toBeTruthy();
    });
    // No select rendered — falls back to plain display.
    expect(screen.queryByTestId("lmstudio-embedding-model-select")).toBeNull();
  });

  it("background-model recommendation: renders when no model is pinned and a suitable LLM is loaded", async () => {
    mockEmbeddingStatusState.data = {
      active_model_id: null,
      loaded_embedding_models: [],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "no_embedding_model",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_background_model_id: null,
      loaded_background_models: [
        { key: "qwen3-8b-general" },
        { key: "coder-qwen3-8b" },
      ],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(
        screen.getByTestId("lmstudio-background-model-recommendation"),
      ).toBeTruthy();
    });
    // Recommendation names the first non-embed model (coder models are now eligible).
    const hint = screen.getByTestId("lmstudio-background-model-recommendation");
    expect(hint.textContent).toContain("qwen3-8b-general");
    expect(hint.textContent?.toLowerCase()).toContain("small, fast");
  });

  it("background-model recommendation: hidden when a model is already pinned", async () => {
    mockEmbeddingStatusState.data = {
      active_model_id: null,
      loaded_embedding_models: [],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "no_embedding_model",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      // User already pinned a model — recommendation must not show.
      preferred_background_model_id: "qwen3-8b-general",
      loaded_background_models: [
        { key: "qwen3-8b-general" },
      ],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-background-model-select")).toBeTruthy();
    });
    expect(
      screen.queryByTestId("lmstudio-background-model-recommendation"),
    ).toBeNull();
  });

  it("background-model recommendation: hidden when only embedding models are loaded", async () => {
    mockEmbeddingStatusState.data = {
      active_model_id: null,
      loaded_embedding_models: [],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "no_embedding_model",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_background_model_id: null,
      // All loaded models are embedding-only — no suitable chat LLM to recommend.
      // Coder models are now eligible (they extract memory fine), so this test
      // uses only embedders to verify the recommendation is absent.
      loaded_background_models: [
        { key: "embed-bge" },
        { key: "text-embedding-nomic" },
      ],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-background-model-select")).toBeTruthy();
    });
    expect(
      screen.queryByTestId("lmstudio-background-model-recommendation"),
    ).toBeNull();
  });

  it("background-model select: excludes only embedding models from the option list", async () => {
    // Coder models are now eligible background-task models (they extract memory
    // fine — verified this cycle). Only embedding models are excluded.
    mockEmbeddingStatusState.data = {
      active_model_id: null,
      loaded_embedding_models: [],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "no_embedding_model",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_background_model_id: null,
      loaded_background_models: [
        { key: "qwen3-8b-general" },
        { key: "qwopus3.5-9b-coder-mtp" },
        { key: "text-embedding-bge-m3" },
      ],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-background-model-select")).toBeTruthy();
    });
    const sel = screen.getByTestId(
      "lmstudio-background-model-select",
    ) as HTMLSelectElement;
    const enabledOptions = Array.from(sel.options).filter((o) => !o.disabled);
    const enabledValues = enabledOptions.map((o) => o.value);
    // General LLM must be present as an enabled option.
    expect(enabledValues).toContain("qwen3-8b-general");
    // Coder model must NOW appear as an enabled option (verified eligible).
    expect(enabledValues).toContain("qwopus3.5-9b-coder-mtp");
    // Only embedding models are excluded — no enabled option should match /embed/i.
    expect(enabledValues.some((v) => /embed/i.test(v))).toBe(false);
    // Verify the embedding model is not in the option list at all (not just disabled).
    const allValues = Array.from(sel.options).map((o) => o.value);
    expect(allValues).not.toContain("text-embedding-bge-m3");
  });

  it("background-model select: shows currently-saved embedding pref as disabled (not silently dropped)", async () => {
    // The disabled-option guard now targets ONLY embedding prefs (/embed/i).
    // A saved coder pref is a NORMAL enabled option (coder models are eligible).
    // A saved embedding pref must still appear as a DISABLED option so the
    // select doesn't silently reset to "" without the admin noticing.
    mockEmbeddingStatusState.data = {
      active_model_id: null,
      loaded_embedding_models: [],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "no_embedding_model",
    };
    mockLmStudioConfigState.data = {
      base_url: "http://localhost:1234",
      default_model: "model",
      api_key_set: false,
      source_base_url: "env",
      source_api_key: "env",
      source_default_model: "env",
      preferred_background_model_id: "text-embedding-bge-m3",
      loaded_background_models: [
        { key: "qwen3-8b-general" },
        { key: "qwopus3.5-9b-coder-mtp" },
        { key: "text-embedding-bge-m3" },
      ],
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-background-model-select")).toBeTruthy();
    });
    const sel = screen.getByTestId(
      "lmstudio-background-model-select",
    ) as HTMLSelectElement;
    // The embedding pref must exist as a DISABLED option (not a chat LLM).
    const embedOption = Array.from(sel.options).find(
      (o) => o.value === "text-embedding-bge-m3",
    );
    expect(embedOption).toBeTruthy();
    expect(embedOption?.disabled).toBe(true);
    // The general LLM is an enabled choice.
    const generalOption = Array.from(sel.options).find(
      (o) => o.value === "qwen3-8b-general",
    );
    expect(generalOption).toBeTruthy();
    expect(generalOption?.disabled).toBeFalsy();
    // The coder model is also an enabled choice (coder models are now eligible).
    const coderOption = Array.from(sel.options).find(
      (o) => o.value === "qwopus3.5-9b-coder-mtp",
    );
    expect(coderOption).toBeTruthy();
    expect(coderOption?.disabled).toBeFalsy();
  });
});
