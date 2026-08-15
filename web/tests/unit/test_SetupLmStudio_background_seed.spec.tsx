/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SetupLmStudio — out-of-box background-tasks model SEED.
 *
 * A fresh install ships with no background-tasks model, so auto-memory
 * distillation / titles / follow-ups fall back to the per-turn chat model —
 * which rotates and is often a coder model that extracts memory poorly. The
 * setup-save handler seeds the FIRST loaded non-coder, non-embed LLM via an
 * explicit PATCH /api/settings/lmstudio/background-model.
 *
 * Locked behaviours:
 *   - Save with no background pref + a loaded non-coder LLM → PATCHes
 *     background-model with that key.
 *   - Save with ONLY a coder model loaded → does NOT PATCH background-model.
 *   - The seed is an explicit save-action PATCH, never a render side-effect.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const mockRequest = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: [path: string, init?: RequestInit]) => mockRequest(...args),
    postForm: vi.fn(),
  },
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

// Admin path → the save PATCHes /api/admin/lmstudio/default and the seed runs.
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

/**
 * Wire mockRequest to respond per path:
 *   - GET  /api/settings/lmstudio          → resolved config (no bg pref).
 *   - POST /api/settings/lmstudio/test     → probe with `models`.
 *   - PATCH /api/admin/lmstudio/default    → ok.
 *   - PATCH .../embedding-model            → ok.
 *   - PATCH .../background-model           → ok (recorded for assertion).
 */
function wireApi(models: ProbedModel[], bgPref: string | null = null): void {
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

  // Save.
  fireEvent.submit(screen.getByTestId("setup-lmstudio-form"));
}

function bgPatchCalls(): unknown[][] {
  return mockRequest.mock.calls.filter(
    (c) => c[0] === "/api/settings/lmstudio/background-model",
  );
}

describe("SetupLmStudio background-model seed", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("seeds the first loaded non-coder, non-embed LLM on save when no pref is set", async () => {
    wireApi([
      { id: "nomic-embed-text", name: "Nomic Embed", loaded: true, is_embedding: true },
      { id: "qwen3-coder-30b", name: "Qwen Coder", loaded: true, is_embedding: false },
      { id: "qwen3-30b", name: "Qwen 3 30B", loaded: true, is_embedding: false },
    ]);

    await driveToSavedState("qwen3-coder-30b");

    await waitFor(() => {
      expect(bgPatchCalls().length).toBe(1);
    });
    const [, init] = bgPatchCalls()[0] as [string, { body: string }];
    // The coder model is skipped; the general model is seeded — even though
    // the admin picked the coder model as the *chat* default.
    expect(JSON.parse(init.body)).toEqual({ background_model_id: "qwen3-30b" });
  });

  it("does NOT seed when only a coder (or embed) model is loaded", async () => {
    wireApi([
      { id: "nomic-embed-text", name: "Nomic Embed", loaded: true, is_embedding: true },
      { id: "qwen3-coder-30b", name: "Qwen Coder", loaded: true, is_embedding: false },
      // A general model exists but is NOT loaded → not a valid background seed.
      { id: "qwen3-30b", name: "Qwen 3 30B", loaded: false, is_embedding: false },
    ]);

    await driveToSavedState("qwen3-coder-30b");

    // Wait for the main config PATCH to land, then assert no bg PATCH fired.
    await waitFor(() => {
      expect(
        mockRequest.mock.calls.some((c) => c[0] === "/api/admin/lmstudio/default"),
      ).toBe(true);
    });
    expect(bgPatchCalls().length).toBe(0);
  });

  it("does NOT clobber an existing background pref on a setup re-run", async () => {
    wireApi(
      [{ id: "qwen3-30b", name: "Qwen 3 30B", loaded: true, is_embedding: false }],
      "already-chosen-model",
    );

    await driveToSavedState("qwen3-30b");

    await waitFor(() => {
      expect(
        mockRequest.mock.calls.some((c) => c[0] === "/api/admin/lmstudio/default"),
      ).toBe(true);
    });
    expect(bgPatchCalls().length).toBe(0);
  });
});
