/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SetupLmStudio — endpoint-mode toggle (native vs OpenAI-compatible).
 *
 * Locked behaviours:
 *   - Default radio selection is "native" (mirrors the BE's default when
 *     nothing has been saved yet).
 *   - Selecting openai_compat before submit causes the save handler to
 *     PATCH /api/settings/lmstudio/endpoint-mode with { endpoint_mode:
 *     "openai_compat" }.
 *   - Leaving it at native still PATCHes with { endpoint_mode: "native" } —
 *     this is an always-fire PATCH (unlike the background-model seed's
 *     "only if unset" gating).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const mockRequest = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args), postForm: vi.fn() },
  ApiClient: vi.fn(),
}));

// useNavigate is called on a successful save (navigate("/")). A no-op spy.
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

// useQueryClient().invalidateQueries is awaited after save.
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn().mockResolvedValue(undefined) }),
}));

vi.mock("@/hooks/useDocumentTitle", () => ({
  useDocumentTitle: () => undefined,
}));

// Admin path → the save PATCHes /api/admin/lmstudio/default.
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = { user: { id: 1, username: "admin", is_admin: true }, isInitializing: false };
    return typeof selector === "function" ? selector(state) : state;
  },
}));

// resolveProbe is a store action invoked from handleTest; a no-op spy.
vi.mock("@/stores/lmStudioStore", () => ({
  useLmStudioStore: (selector: (s: { resolveProbe: () => void }) => unknown) =>
    selector({ resolveProbe: vi.fn() }),
}));

// Query-key barrels imported by the page for invalidation.
vi.mock("@/hooks/useModels", () => ({ modelKeys: { list: () => ["models"] } }));
vi.mock("@/hooks/useLmStudioConfig", () => ({
  lmStudioConfigKeys: { resolved: () => ["lmstudio-config", "resolved"] },
}));
vi.mock("@/hooks/useEmbeddingStatus", () => ({
  embeddingStatusKeys: { current: () => ["embedding-status"] },
}));

interface ProbedModel {
  id: string;
  name: string;
  loaded: boolean;
  is_embedding: boolean;
}

const NATIVE_BLURB =
  "LM Studio runs your MCP tools itself (from ~/.lmstudio/mcp.json) and keeps the conversation on its side, so LM Chat sends less each turn — faster on long chats. Best when LM Chat and LM Studio are on the same machine.";
const OPENAI_COMPAT_BLURB =
  "LM Chat runs tools through its own MCP Store — 1-click install, no mcp.json editing, and it works even when LM Studio is on another machine. Trade-off: the conversation is resent each turn (no server-side chaining), so very long chats cost a little more.";

/**
 * Wire mockRequest to respond per path:
 *   - GET  /api/settings/lmstudio          → resolved config.
 *   - POST /api/settings/lmstudio/test     → probe with `models`.
 *   - all PATCH endpoints                  → ok (recorded for assertion).
 */
function wireApi(
  models: ProbedModel[],
  opts: {
    bgPref?: string | null;
    endpointMode?: "native" | "openai_compat";
  } = {},
): void {
  const { bgPref = "already-chosen-model", endpointMode } = opts;
  mockRequest.mockImplementation((path: string, init?: { method?: string }) => {
    if (path === "/api/settings/lmstudio" && (init?.method ?? "GET") === "GET") {
      return Promise.resolve({
        base_url: "http://localhost:1234",
        default_model: "",
        api_key_set: false,
        source_base_url: "env",
        source_api_key: "env",
        source_default_model: "env",
        preferred_background_model_id: bgPref,
        ...(endpointMode !== undefined ? { lm_studio_endpoint_mode: endpointMode } : {}),
      });
    }
    if (path === "/api/settings/lmstudio/test") {
      return Promise.resolve({ ok: true, model_count: models.length, models });
    }
    // All PATCH endpoints just resolve.
    return Promise.resolve({});
  });
}

async function driveToSavedState(defaultModelId: string): Promise<void> {
  vi.resetModules();
  const { default: SetupLmStudio } = await import("@/pages/SetupLmStudio");
  render(<SetupLmStudio />);

  // The initial GET pre-fills the base URL (source env). Wait for it.
  await waitFor(() => {
    const url = screen.getByTestId("setup-lmstudio-base-url") as HTMLInputElement;
    expect(url.value).toBe("http://localhost:1234");
  });

  // Probe.
  fireEvent.click(screen.getByTestId("setup-lmstudio-test-connection"));
  await waitFor(() => {
    expect(screen.getByTestId("setup-lmstudio-probe-result")).toBeTruthy();
  });

  // Pick a default chat model (unlocks Save).
  fireEvent.change(screen.getByTestId("setup-lmstudio-default-model"), {
    target: { value: defaultModelId },
  });
}

function endpointModePatchCalls(): unknown[][] {
  return mockRequest.mock.calls.filter(
    (c) => c[0] === "/api/settings/lmstudio/endpoint-mode",
  );
}

const MODELS: ProbedModel[] = [
  { id: "nomic-embed-text", name: "Nomic Embed", loaded: true, is_embedding: true },
  { id: "qwen3-30b", name: "Qwen 3 30B", loaded: true, is_embedding: false },
];

describe("SetupLmStudio endpoint-mode toggle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("renders both radio options with their verbatim blurbs; default selection is native", async () => {
    wireApi(MODELS);
    await driveToSavedState("qwen3-30b");

    const native = screen.getByTestId("lmstudio-endpoint-mode-native") as HTMLInputElement;
    const compat = screen.getByTestId(
      "lmstudio-endpoint-mode-openai-compat",
    ) as HTMLInputElement;
    expect(native.checked).toBe(true);
    expect(compat.checked).toBe(false);
    expect(screen.getByText(NATIVE_BLURB)).toBeTruthy();
    expect(screen.getByText(OPENAI_COMPAT_BLURB)).toBeTruthy();
  });

  it("reflects openai_compat as the seeded default when the resolved config says so", async () => {
    wireApi(MODELS, { endpointMode: "openai_compat" });
    await driveToSavedState("qwen3-30b");

    const compat = screen.getByTestId(
      "lmstudio-endpoint-mode-openai-compat",
    ) as HTMLInputElement;
    expect(compat.checked).toBe(true);
  });

  it("selecting openai_compat before submit PATCHes endpoint-mode with openai_compat", async () => {
    wireApi(MODELS);
    await driveToSavedState("qwen3-30b");

    fireEvent.click(screen.getByTestId("lmstudio-endpoint-mode-openai-compat"));
    fireEvent.submit(screen.getByTestId("setup-lmstudio-form"));

    await waitFor(() => {
      expect(endpointModePatchCalls().length).toBe(1);
    });
    const [, init] = endpointModePatchCalls()[0] as [string, { method?: string; body: string }];
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ endpoint_mode: "openai_compat" });
  });

  it("leaving it at native still PATCHes with native — always-fire, not gated", async () => {
    wireApi(MODELS);
    await driveToSavedState("qwen3-30b");

    // No click on the radio — native stays selected (the default).
    fireEvent.submit(screen.getByTestId("setup-lmstudio-form"));

    await waitFor(() => {
      expect(endpointModePatchCalls().length).toBe(1);
    });
    const [, init] = endpointModePatchCalls()[0] as [string, { method?: string; body: string }];
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ endpoint_mode: "native" });
  });
});
