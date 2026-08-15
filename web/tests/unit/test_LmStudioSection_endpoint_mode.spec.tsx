/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for EndpointModeCard (LmStudioSection.tsx) — the native vs
 * OpenAI-compatible endpoint-mode toggle.
 *
 * Locked behaviours:
 *   - Both radio options render, each with its own always-visible info
 *     blurb (verbatim copy).
 *   - Default selection reflects `lm_studio_endpoint_mode` from the
 *     resolved config (falls back to "native" when absent/unset).
 *   - Selecting the other option PATCHes /api/settings/lmstudio/endpoint-mode
 *     with { endpoint_mode } and invalidates lmStudioConfigKeys.resolved().
 *   - A failed PATCH surfaces the error in the inline error region.
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

vi.mock("@/hooks/useModels", () => ({
  useRefreshModels: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
}));

// ─── Mock useChatModelOptions ─────────────────────────────────────────────────

vi.mock("@/hooks/useChatModelOptions", () => ({
  useChatModelOptions: () => ({
    options: [],
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

// ─── Mock useAuthStore — admin user (Test/Save buttons are admin-gated) ──────

vi.mock("@/stores/authStore", () => {
  const state = {
    user: { is_admin: true } as { is_admin: boolean } | null,
    isInitializing: false,
  };
  return {
    useAuthStore: (selector?: (s: typeof state) => unknown) =>
      selector !== undefined ? selector(state) : state,
  };
});

// ─── Mock useEmbeddingStatus — keeps MemoryIndexingCard quiet ────────────────

vi.mock("@/hooks/useEmbeddingStatus", () => ({
  useEmbeddingStatus: () => ({ data: undefined, isLoading: false, isError: false }),
}));

// ─── Mock useLmStudioConfig — the hook EndpointModeCard reads from ───────────

const mockLmStudioConfigState = vi.hoisted(() => ({
  data: undefined as
    | {
        base_url: string;
        default_model: string;
        api_key_set: boolean;
        source_base_url: string;
        source_api_key: string;
        source_default_model: string;
        lm_studio_endpoint_mode?: "native" | "openai_compat";
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

const NATIVE_BLURB =
  "LM Studio runs your MCP tools itself (from ~/.lmstudio/mcp.json) and keeps the conversation on its side, so LM Chat sends less each turn — faster on long chats. Best when LM Chat and LM Studio are on the same machine.";
const OPENAI_COMPAT_BLURB =
  "LM Chat runs tools through its own MCP Store — 1-click install, no mcp.json editing, and it works even when LM Studio is on another machine. Trade-off: the conversation is resent each turn (no server-side chaining), so very long chats cost a little more.";

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
 * (createBrowserRouter / createMemoryRouter).
 */
function renderInDataRouter(Component: () => any) {
  const router = createMemoryRouter(
    [{ path: "/", element: createElement(Component) }],
    { initialEntries: ["/"] },
  );
  return render(createElement(RouterProvider, { router }));
}

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("EndpointModeCard (LmStudioSection)", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockPush.mockClear();
    mockInvalidateQueries.mockClear();
    mockInvalidateQueries.mockResolvedValue(undefined);
    mockLmStudioConfigState.data = undefined;
    cleanup();
  });

  it("renders both radio options with their verbatim info blurbs, always visible", async () => {
    mockLmStudioConfigState.data = { ...RESOLVED_ENV, lm_studio_endpoint_mode: "native" };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-endpoint-mode-card")).toBeTruthy();
    });

    expect(screen.getByTestId("lmstudio-endpoint-mode-native")).toBeTruthy();
    expect(screen.getByTestId("lmstudio-endpoint-mode-openai-compat")).toBeTruthy();
    // Both blurbs are visible simultaneously — not gated on selection.
    expect(screen.getByText(NATIVE_BLURB)).toBeTruthy();
    expect(screen.getByText(OPENAI_COMPAT_BLURB)).toBeTruthy();
  });

  it("defaults to native when lm_studio_endpoint_mode is absent", async () => {
    mockLmStudioConfigState.data = { ...RESOLVED_ENV }; // no lm_studio_endpoint_mode field
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-endpoint-mode-card")).toBeTruthy();
    });

    const native = screen.getByTestId("lmstudio-endpoint-mode-native") as HTMLInputElement;
    const compat = screen.getByTestId(
      "lmstudio-endpoint-mode-openai-compat",
    ) as HTMLInputElement;
    expect(native.checked).toBe(true);
    expect(compat.checked).toBe(false);
  });

  it("reflects openai_compat as the default selection when the resolved config says so", async () => {
    mockLmStudioConfigState.data = {
      ...RESOLVED_ENV,
      lm_studio_endpoint_mode: "openai_compat",
    };
    global.fetch = vi.fn().mockResolvedValue(jsonResponse(RESOLVED_ENV));
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-endpoint-mode-card")).toBeTruthy();
    });

    const native = screen.getByTestId("lmstudio-endpoint-mode-native") as HTMLInputElement;
    const compat = screen.getByTestId(
      "lmstudio-endpoint-mode-openai-compat",
    ) as HTMLInputElement;
    expect(compat.checked).toBe(true);
    expect(native.checked).toBe(false);
  });

  it("clicking the other option PATCHes endpoint-mode and invalidates the config query", async () => {
    mockLmStudioConfigState.data = { ...RESOLVED_ENV, lm_studio_endpoint_mode: "native" };
    const fetchMock = vi
      .fn()
      // Initial GET /api/settings/lmstudio (LmStudioSection form mount)
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      // PATCH /api/settings/lmstudio/endpoint-mode
      .mockResolvedValueOnce(jsonResponse({ endpoint_mode: "openai_compat" }));
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-endpoint-mode-card")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("lmstudio-endpoint-mode-openai-compat"));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith({
        variant: "success",
        message: "Endpoint mode saved.",
      });
    });

    const patchCall = (fetchMock.mock.calls as [string, RequestInit | undefined][]).find(
      ([url]) => url === "/api/settings/lmstudio/endpoint-mode",
    );
    expect(patchCall).toBeTruthy();
    const init = patchCall?.[1];
    expect(init?.method).toBe("PATCH");
    const body = JSON.parse(init?.body as string);
    expect(body).toEqual({ endpoint_mode: "openai_compat" });

    expect(mockInvalidateQueries).toHaveBeenCalledWith({
      queryKey: ["lmstudio-config", "resolved"],
    });
  });

  it("disables both inputs while the PATCH is pending", async () => {
    mockLmStudioConfigState.data = { ...RESOLVED_ENV, lm_studio_endpoint_mode: "native" };
    let resolvePatch!: (value: Response) => void;
    const patchPromise = new Promise<Response>((resolve) => {
      resolvePatch = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      .mockReturnValueOnce(patchPromise);
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-endpoint-mode-card")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("lmstudio-endpoint-mode-openai-compat"));

    await waitFor(() => {
      expect(
        (screen.getByTestId("lmstudio-endpoint-mode-native") as HTMLInputElement).disabled,
      ).toBe(true);
    });
    expect(
      (screen.getByTestId("lmstudio-endpoint-mode-openai-compat") as HTMLInputElement).disabled,
    ).toBe(true);

    // Let the pending PATCH resolve so the test doesn't leak a dangling promise.
    resolvePatch(jsonResponse({ endpoint_mode: "openai_compat" }));
    await waitFor(() => {
      expect(mockPush).toHaveBeenCalled();
    });
  });

  it("an error response surfaces the error testid and does not toast success", async () => {
    mockLmStudioConfigState.data = { ...RESOLVED_ENV, lm_studio_endpoint_mode: "native" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(RESOLVED_ENV))
      .mockResolvedValueOnce(
        jsonResponse({ detail: "Invalid endpoint_mode value" }, 422),
      );
    global.fetch = fetchMock;
    const Section = await freshSection();
    renderInDataRouter(Section);

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-endpoint-mode-card")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("lmstudio-endpoint-mode-openai-compat"));

    await waitFor(() => {
      expect(screen.getByTestId("lmstudio-endpoint-mode-error")).toBeTruthy();
    });
    expect(screen.getByTestId("lmstudio-endpoint-mode-error").textContent).toContain(
      "Invalid endpoint_mode value",
    );
    expect(mockPush).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: "success" }),
    );
  });
});
