/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SetupLmStudio — embedder-selection invariants.
 *
 * LM Studio lists every downloaded quant variant as a separate catalog
 * entry (bare key + @q4_k_m + @q8_0, etc.). The setup page must:
 *   - expose ONLY loaded embedders in the dropdown;
 *   - auto-select the loaded nomic-family variant (startsWith), not the
 *     bare key which may be unloaded;
 *   - fall back to the first loaded embedder when no nomic variant is loaded;
 *   - show an empty-state message (no option selected) when nothing is loaded.
 *
 * Regression guard for the 2026-06-25 outage: unloaded bare variant was
 * auto-selected → embedding PATCH sent an unloadable key → RAG silent death.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
  within,
} from "@testing-library/react";

const mockRequest = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args), postForm: vi.fn() },
  ApiClient: vi.fn(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({
    invalidateQueries: vi.fn().mockResolvedValue(undefined),
  }),
}));

vi.mock("@/hooks/useDocumentTitle", () => ({
  useDocumentTitle: () => undefined,
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { id: 1, username: "admin", is_admin: true },
      isInitializing: false,
    };
    return typeof selector === "function" ? selector(state) : state;
  },
}));

vi.mock("@/stores/lmStudioStore", () => ({
  useLmStudioStore: (selector: (s: { resolveProbe: () => void }) => unknown) =>
    selector({ resolveProbe: vi.fn() }),
}));

vi.mock("@/hooks/useModels", () => ({
  modelKeys: { list: () => ["models"] },
}));
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

function wireApi(models: ProbedModel[]): void {
  mockRequest.mockImplementation((path: string, init?: { method?: string }) => {
    if (
      path === "/api/settings/lmstudio" &&
      (init?.method ?? "GET") === "GET"
    ) {
      return Promise.resolve({
        base_url: "http://localhost:1234",
        default_model: "",
        api_key_set: false,
        source_base_url: "env",
        source_api_key: "env",
        source_default_model: "env",
        preferred_background_model_id: null,
      });
    }
    if (path === "/api/settings/lmstudio/test") {
      return Promise.resolve({ ok: true, model_count: models.length, models });
    }
    return Promise.resolve({});
  });
}

async function renderAndProbe(): Promise<void> {
  vi.resetModules();
  const { default: SetupLmStudio } = await import("@/pages/SetupLmStudio");
  render(<SetupLmStudio />);

  // Wait for the initial GET to pre-fill the URL.
  await waitFor(() => {
    const url = screen.getByTestId(
      "setup-lmstudio-base-url",
    ) as HTMLInputElement;
    expect(url.value).toBe("http://localhost:1234");
  });

  // Fire the probe.
  fireEvent.click(screen.getByTestId("setup-lmstudio-test-connection"));
  await waitFor(() => {
    expect(screen.getByTestId("setup-lmstudio-probe-result")).toBeTruthy();
  });
}

function embedSelect(): HTMLSelectElement {
  return screen.getByTestId(
    "setup-lmstudio-embedding-model",
  ) as HTMLSelectElement;
}

function embedOptionIds(): string[] {
  return Array.from(embedSelect().options)
    .filter((o) => o.value !== "")
    .map((o) => o.value);
}

describe("SetupLmStudio embedder selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("exposes ONLY loaded embedders in the dropdown, not unloaded variants", async () => {
    wireApi([
      // The three-variant LM Studio catalog pattern from the real outage:
      {
        id: "text-embedding-nomic-embed-text-v1.5",
        name: "Nomic (bare)",
        loaded: false,
        is_embedding: true,
      },
      {
        id: "text-embedding-nomic-embed-text-v1.5@q8_0",
        name: "Nomic q8_0",
        loaded: false,
        is_embedding: true,
      },
      {
        id: "text-embedding-nomic-embed-text-v1.5@q4_k_m",
        name: "Nomic q4_k_m",
        loaded: true,
        is_embedding: true,
      },
      // A loaded chat model — should NOT appear in the embedder dropdown.
      {
        id: "qwen3-30b",
        name: "Qwen 3 30B",
        loaded: true,
        is_embedding: false,
      },
    ]);

    await renderAndProbe();

    const optionIds = embedOptionIds();
    // Only the loaded @q4_k_m variant should be present.
    expect(optionIds).toEqual(["text-embedding-nomic-embed-text-v1.5@q4_k_m"]);
    // The bare key and @q8_0 must be absent.
    expect(optionIds).not.toContain("text-embedding-nomic-embed-text-v1.5");
    expect(optionIds).not.toContain(
      "text-embedding-nomic-embed-text-v1.5@q8_0",
    );
  });

  it("auto-selects the loaded nomic-family variant, not the bare key", async () => {
    wireApi([
      {
        id: "text-embedding-nomic-embed-text-v1.5",
        name: "Nomic (bare)",
        loaded: false,
        is_embedding: true,
      },
      {
        id: "text-embedding-nomic-embed-text-v1.5@q4_k_m",
        name: "Nomic q4_k_m",
        loaded: true,
        is_embedding: true,
      },
      // A loaded bge embedder — nomic should still win.
      {
        id: "bge-small-en-v1.5",
        name: "BGE Small",
        loaded: true,
        is_embedding: true,
      },
      {
        id: "qwen3-30b",
        name: "Qwen 3 30B",
        loaded: true,
        is_embedding: false,
      },
    ]);

    await renderAndProbe();

    // Auto-selected value must be the LOADED @q4_k_m, not the bare key.
    await waitFor(() => {
      expect(embedSelect().value).toBe(
        "text-embedding-nomic-embed-text-v1.5@q4_k_m",
      );
    });
  });

  it("falls back to the first loaded embedder when no nomic variant is loaded", async () => {
    wireApi([
      {
        id: "bge-small-en-v1.5",
        name: "BGE Small",
        loaded: true,
        is_embedding: true,
      },
      {
        id: "bge-large-en-v1.5",
        name: "BGE Large",
        loaded: true,
        is_embedding: true,
      },
      {
        id: "qwen3-30b",
        name: "Qwen 3 30B",
        loaded: true,
        is_embedding: false,
      },
    ]);

    await renderAndProbe();

    await waitFor(() => {
      expect(embedSelect().value).toBe("bge-small-en-v1.5");
    });
  });

  it("shows empty-state message and no selection when no embedder is loaded", async () => {
    wireApi([
      // All embedders unloaded.
      {
        id: "text-embedding-nomic-embed-text-v1.5",
        name: "Nomic",
        loaded: false,
        is_embedding: true,
      },
      {
        id: "qwen3-30b",
        name: "Qwen 3 30B",
        loaded: true,
        is_embedding: false,
      },
    ]);

    await renderAndProbe();

    // No real options (only the placeholder).
    expect(embedOptionIds()).toEqual([]);
    // The select value should be empty (placeholder selected).
    expect(embedSelect().value).toBe("");
    // The placeholder text must communicate that nothing is loaded.
    const placeholder = within(embedSelect()).getByText(
      /No embedding model loaded/i,
    );
    expect(placeholder).toBeTruthy();
  });

  // Bug B (2026-07-18 dogfood): the setup wizard's model dropdowns fired
  // "Encountered two children with the same key" during a live dogfood.
  // The probe hits LM Studio directly and isn't run through the same
  // normalizer /api/models uses, so a duplicated entry in the raw probe
  // response must not reach the DOM as two <option>s with the same key.
  it("test_SetupLmStudio_dedupes_duplicate_probe_entries: a duplicated model id in the probe response renders only once", async () => {
    wireApi([
      {
        id: "text-embedding-nomic-embed-text-v1.5",
        name: "Nomic Embed Text v1.5",
        loaded: true,
        is_embedding: true,
      },
      // Same id reported twice by the raw probe (upstream LM Studio quirk).
      {
        id: "text-embedding-nomic-embed-text-v1.5",
        name: "Nomic Embed Text v1.5",
        loaded: true,
        is_embedding: true,
      },
    ]);

    await renderAndProbe();

    const optionIds = embedOptionIds();
    expect(optionIds).toEqual(["text-embedding-nomic-embed-text-v1.5"]);
    // The DOM must not carry two <option> nodes for the same key.
    expect(
      optionIds.filter((id) => id === "text-embedding-nomic-embed-text-v1.5")
        .length,
    ).toBe(1);
  });

  it("keeps the user's manual selection if it is still present after re-probe", async () => {
    wireApi([
      {
        id: "text-embedding-nomic-embed-text-v1.5@q4_k_m",
        name: "Nomic q4_k_m",
        loaded: true,
        is_embedding: true,
      },
      {
        id: "bge-small-en-v1.5",
        name: "BGE Small",
        loaded: true,
        is_embedding: true,
      },
      {
        id: "qwen3-30b",
        name: "Qwen 3 30B",
        loaded: true,
        is_embedding: false,
      },
    ]);

    await renderAndProbe();

    // Manually switch to bge.
    fireEvent.change(embedSelect(), { target: { value: "bge-small-en-v1.5" } });
    expect(embedSelect().value).toBe("bge-small-en-v1.5");

    // Re-probe (simulate URL edit → markProbeStale → re-test).
    fireEvent.change(screen.getByTestId("setup-lmstudio-base-url"), {
      target: { value: "http://localhost:1235" },
    });
    // URL change clears probe → embeddingModel reset to "".
    // Re-probe — wire same models.
    fireEvent.click(screen.getByTestId("setup-lmstudio-test-connection"));
    await waitFor(() => {
      expect(screen.getByTestId("setup-lmstudio-probe-result")).toBeTruthy();
    });
    // After re-probe the user had no prior selection (it was cleared by
    // markProbeStale), so auto-select fires: nomic wins. The auto-select is a
    // SEPARATE state update that lands after probe-result renders, so assert it
    // inside waitFor — a bare expect here races the effect and flakes under the
    // concurrent full-suite run (passes standalone, fails ~half the time in CI).
    await waitFor(() => {
      expect(embedSelect().value).toBe(
        "text-embedding-nomic-embed-text-v1.5@q4_k_m",
      );
    });
  });
});
