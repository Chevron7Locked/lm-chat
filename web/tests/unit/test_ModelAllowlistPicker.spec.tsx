/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests — model allowlist picker in ProvidersSection.
 *
 * Locked behaviours:
 *   - Picker appears after test probe returns model_ids (ok=true).
 *   - Picker does NOT appear when probe fails (ok=false).
 *   - Filter input narrows the visible list by substring match.
 *   - "Select all" checks every currently-filtered item.
 *   - "Select none" unchecks every currently-filtered item.
 *   - With filter active, "Select all / Select none" only affects visible items.
 *   - Count label shows "Allowing N of M models" when selection is non-empty.
 *   - Count label shows "All M models allowed (none selected)" when empty.
 *   - "Leave empty to allow all" hint shown only when selection is empty.
 *   - Save includes allowed_models: [] when no models selected.
 *   - Save includes allowed_models: ["id-a"] when one model selected.
 *   - Row badge "N models" appears when allowed_models is non-empty.
 *   - Row badge absent when allowed_models is null / empty.
 *   - Edit mode: pre-checks existing allowed_models from config.
 *   - Edit mode + existing allowlist but no re-test: shows "retest" hint.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { createElement } from "react";

// ─── Mock toast ──────────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
  useToastStore: { getState: () => ({ push: mockPush }) },
}));

// ─── Mock authStore ───────────────────────────────────────────────────────────

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

// ─── Hoisted mutable state for hook mocks ────────────────────────────────────

const mockUpsertMutate = vi.fn();
const mockDeleteMutate = vi.fn();
const mockTestMutate = vi.fn();

const mockProvidersState = vi.hoisted(() => ({
  providers: [] as {
    provider: string;
    base_url: string;
    default_model: string | null;
    extra_headers: Record<string, string> | null;
    enabled: boolean;
    api_key_set: boolean;
    allowed_models?: string[] | null;
  }[],
  status: [] as { provider: string; reachable: boolean; error: string | null }[],
  upsertPending: false as boolean,
  deletePending: false as boolean,
  testPending: false as boolean,
}));

vi.mock("@/hooks/useProviders", () => ({
  useProviders: () => ({
    data: mockProvidersState.providers,
    isLoading: false,
    isError: false,
  }),
  useProviderStatus: () => ({
    data: mockProvidersState.status,
    isLoading: false,
    isError: false,
  }),
  useUpsertProvider: () => ({
    mutate: mockUpsertMutate,
    isPending: mockProvidersState.upsertPending,
  }),
  useDeleteProvider: () => ({
    mutate: mockDeleteMutate,
    isPending: mockProvidersState.deletePending,
  }),
  useTestProvider: () => ({
    mutate: mockTestMutate,
    isPending: mockProvidersState.testPending,
  }),
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const OPENROUTER_CONFIG = {
  provider: "openrouter",
  base_url: "https://openrouter.ai/api",
  default_model: null,
  extra_headers: null,
  enabled: true,
  api_key_set: true,
  allowed_models: null,
};

const SAMPLE_MODEL_IDS = [
  "openai/gpt-4o",
  "openai/gpt-4o-mini",
  "anthropic/claude-3-haiku",
  "anthropic/claude-3-sonnet",
  "google/gemini-flash",
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function freshSection() {
  vi.resetModules();
  const mod = await import("@/components/ProvidersSection");
  return mod.ProvidersSection;
}

/** Renders the section, clicks the given provider row to open edit form. */
async function openEditForm(Section: React.ComponentType, providerTestId = "provider-row-openrouter") {
  render(createElement(Section));
  await waitFor(() => {
    expect(screen.getByTestId("settings-providers-section")).toBeTruthy();
  });
  fireEvent.click(screen.getByTestId(providerTestId));
  await waitFor(() => {
    expect(screen.getByTestId("providers-form")).toBeTruthy();
  });
}

/** Simulates a successful test probe returning model_ids. */
function mockTestSuccess(modelIds: string[] = SAMPLE_MODEL_IDS) {
  mockTestMutate.mockImplementation((_vars: unknown, opts: { onSuccess?: (r: unknown) => void }) => {
    opts?.onSuccess?.({
      ok: true,
      model_count: modelIds.length,
      model_ids: modelIds,
      error: null,
    });
  });
}

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("ModelAllowlistPicker", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockPush.mockClear();
    mockUpsertMutate.mockClear();
    mockDeleteMutate.mockClear();
    mockTestMutate.mockClear();
    mockProvidersState.providers = [];
    mockProvidersState.status = [];
    mockProvidersState.upsertPending = false;
    mockProvidersState.deletePending = false;
    mockProvidersState.testPending = false;
    cleanup();
  });

  // ── Picker visibility ──────────────────────────────────────────────────────

  it("picker not shown before test probe", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    const Section = await freshSection();
    await openEditForm(Section);

    expect(screen.queryByTestId("allowlist-picker")).toBeNull();
  });

  it("picker appears after successful test probe with model_ids", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);

    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-picker")).toBeTruthy();
    });
  });

  it("picker does NOT appear when probe fails (ok=false)", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestMutate.mockImplementation((_vars: unknown, opts: { onSuccess?: (r: unknown) => void }) => {
      opts?.onSuccess?.({ ok: false, model_count: null, model_ids: null, error: "unauthorized" });
    });

    const Section = await freshSection();
    await openEditForm(Section);

    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-test-result")).toBeTruthy();
    });

    expect(screen.queryByTestId("allowlist-picker")).toBeNull();
  });

  // ── Empty-selection semantics ──────────────────────────────────────────────

  it("empty selection shows 'All M models allowed (none selected)'", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-picker")).toBeTruthy();
    });

    const countEl = screen.getByTestId("allowlist-count");
    expect(countEl.textContent).toContain("All");
    expect(countEl.textContent).toContain(String(SAMPLE_MODEL_IDS.length));
    expect(countEl.textContent).toContain("none selected");
  });

  it("empty selection shows 'leave empty to allow all' hint", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-all-hint")).toBeTruthy();
    });

    expect(screen.getByTestId("allowlist-all-hint").textContent).toContain("Leave empty");
  });

  it("non-empty selection hides the 'leave empty' hint and shows Allowing N of M", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-picker")).toBeTruthy();
    });

    // Check one model
    fireEvent.click(screen.getByTestId("allowlist-checkbox-openai/gpt-4o"));

    await waitFor(() => {
      const countEl = screen.getByTestId("allowlist-count");
      expect(countEl.textContent).toContain("Allowing 1 of");
    });

    expect(screen.queryByTestId("allowlist-all-hint")).toBeNull();
  });

  // ── Filter ─────────────────────────────────────────────────────────────────

  it("filter narrows the visible list by substring", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-filter")).toBeTruthy();
    });

    // Before filter: all items visible
    const listBefore = screen.getByTestId("allowlist-list");
    expect(listBefore.querySelectorAll("input[type=checkbox]").length).toBe(SAMPLE_MODEL_IDS.length);

    // Type filter
    fireEvent.change(screen.getByTestId("allowlist-filter"), { target: { value: "openai" } });

    await waitFor(() => {
      const listAfter = screen.getByTestId("allowlist-list");
      // Only openai/gpt-4o and openai/gpt-4o-mini match
      expect(listAfter.querySelectorAll("input[type=checkbox]").length).toBe(2);
    });
  });

  it("filter is case-insensitive", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-filter")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("allowlist-filter"), { target: { value: "ANTHROPIC" } });

    await waitFor(() => {
      const list = screen.getByTestId("allowlist-list");
      expect(list.querySelectorAll("input[type=checkbox]").length).toBe(2);
    });
  });

  // ── Select all / Select none ───────────────────────────────────────────────

  it("Select all checks all items (no filter active)", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-select-all")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("allowlist-select-all"));

    await waitFor(() => {
      const countEl = screen.getByTestId("allowlist-count");
      expect(countEl.textContent).toContain(`Allowing ${String(SAMPLE_MODEL_IDS.length)} of`);
    });
  });

  it("Select none unchecks all items", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-select-all")).toBeTruthy();
    });

    // Select all first
    fireEvent.click(screen.getByTestId("allowlist-select-all"));
    await waitFor(() => {
      const countEl = screen.getByTestId("allowlist-count");
      expect(countEl.textContent).toContain("Allowing");
    });

    // Then select none
    fireEvent.click(screen.getByTestId("allowlist-select-none"));
    await waitFor(() => {
      const countEl = screen.getByTestId("allowlist-count");
      expect(countEl.textContent).toContain("All");
      expect(countEl.textContent).toContain("none selected");
    });
  });

  it("Select all with filter only selects the filtered subset", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-filter")).toBeTruthy();
    });

    // Filter to openai only (2 models)
    fireEvent.change(screen.getByTestId("allowlist-filter"), { target: { value: "openai" } });

    await waitFor(() => {
      const list = screen.getByTestId("allowlist-list");
      expect(list.querySelectorAll("input[type=checkbox]").length).toBe(2);
    });

    fireEvent.click(screen.getByTestId("allowlist-select-all"));

    await waitFor(() => {
      const countEl = screen.getByTestId("allowlist-count");
      // Should be 2 of 5 total
      expect(countEl.textContent).toContain("Allowing 2 of");
    });
  });

  it("Select none with filter only deselects the filtered subset", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-select-all")).toBeTruthy();
    });

    // Select all 5 first
    fireEvent.click(screen.getByTestId("allowlist-select-all"));
    await waitFor(() => {
      expect(screen.getByTestId("allowlist-count").textContent).toContain("Allowing 5 of");
    });

    // Filter to openai (2 models)
    fireEvent.change(screen.getByTestId("allowlist-filter"), { target: { value: "openai" } });

    await waitFor(() => {
      const list = screen.getByTestId("allowlist-list");
      expect(list.querySelectorAll("input[type=checkbox]").length).toBe(2);
    });

    // Deselect none (filtered) — removes openai/gpt-4o and openai/gpt-4o-mini
    fireEvent.click(screen.getByTestId("allowlist-select-none"));

    // Clear filter to see total count
    fireEvent.change(screen.getByTestId("allowlist-filter"), { target: { value: "" } });

    await waitFor(() => {
      const countEl = screen.getByTestId("allowlist-count");
      // 5 - 2 = 3 remaining
      expect(countEl.textContent).toContain("Allowing 3 of");
    });
  });

  // ── Save body ──────────────────────────────────────────────────────────────

  it("save with no models selected sends allowed_models: []", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();
    mockUpsertMutate.mockImplementation((_vars: unknown, opts: { onSuccess?: () => void }) => {
      opts?.onSuccess?.();
    });

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-picker")).toBeTruthy();
    });

    // Do not check any models, click Save directly
    fireEvent.click(screen.getByTestId("providers-save"));

    await waitFor(() => {
      expect(mockUpsertMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            allowed_models: [],
          }),
        }),
        expect.any(Object),
      );
    });
  });

  it("save with selected models sends correct allowed_models array", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess();
    mockUpsertMutate.mockImplementation((_vars: unknown, opts: { onSuccess?: () => void }) => {
      opts?.onSuccess?.();
    });

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-picker")).toBeTruthy();
    });

    // Check two models
    fireEvent.click(screen.getByTestId("allowlist-checkbox-openai/gpt-4o"));
    fireEvent.click(screen.getByTestId("allowlist-checkbox-google/gemini-flash"));

    fireEvent.click(screen.getByTestId("providers-save"));

    await waitFor(() => {
      expect(mockUpsertMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            allowed_models: expect.arrayContaining(["openai/gpt-4o", "google/gemini-flash"]),
          }),
        }),
        expect.any(Object),
      );
    });

    // Exactly 2 items
    const callBody = (mockUpsertMutate.mock.calls[0] as [{ body: { allowed_models: string[] } }])[0].body;
    expect(callBody.allowed_models).toHaveLength(2);
  });

  it("save without running test still sends allowed_models: [] (no picker shown)", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockUpsertMutate.mockImplementation((_vars: unknown, opts: { onSuccess?: () => void }) => {
      opts?.onSuccess?.();
    });

    const Section = await freshSection();
    await openEditForm(Section);

    // Save without testing
    fireEvent.click(screen.getByTestId("providers-save"));

    await waitFor(() => {
      expect(mockUpsertMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({
            allowed_models: [],
          }),
        }),
        expect.any(Object),
      );
    });
  });

  // ── Row badge ─────────────────────────────────────────────────────────────

  it("row badge shown when provider has non-empty allowed_models", async () => {
    mockProvidersState.providers = [
      { ...OPENROUTER_CONFIG, allowed_models: ["openai/gpt-4o", "google/gemini-flash"] },
    ];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-allowlist-badge-openrouter")).toBeTruthy();
    });

    expect(screen.getByTestId("provider-allowlist-badge-openrouter").textContent).toContain("2 models");
  });

  it("row badge absent when allowed_models is null", async () => {
    mockProvidersState.providers = [{ ...OPENROUTER_CONFIG, allowed_models: null }];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    expect(screen.queryByTestId("provider-allowlist-badge-openrouter")).toBeNull();
  });

  it("row badge absent when allowed_models is empty array", async () => {
    mockProvidersState.providers = [{ ...OPENROUTER_CONFIG, allowed_models: [] }];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    expect(screen.queryByTestId("provider-allowlist-badge-openrouter")).toBeNull();
  });

  it("row badge uses singular 'model' for count of 1", async () => {
    mockProvidersState.providers = [
      { ...OPENROUTER_CONFIG, allowed_models: ["openai/gpt-4o"] },
    ];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-allowlist-badge-openrouter")).toBeTruthy();
    });

    expect(screen.getByTestId("provider-allowlist-badge-openrouter").textContent).toContain("1 model");
    expect(screen.getByTestId("provider-allowlist-badge-openrouter").textContent).not.toContain("models");
  });

  // ── Edit mode with existing allowlist ─────────────────────────────────────

  it("edit mode pre-checks existing allowed_models after successful re-test", async () => {
    const existingConfig = {
      ...OPENROUTER_CONFIG,
      allowed_models: ["openai/gpt-4o"],
    };
    mockProvidersState.providers = [existingConfig];
    mockTestSuccess(SAMPLE_MODEL_IDS);

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-picker")).toBeTruthy();
    });

    // Count should reflect the pre-checked model
    await waitFor(() => {
      const countEl = screen.getByTestId("allowlist-count");
      expect(countEl.textContent).toContain("Allowing 1 of");
    });

    // The pre-existing model should be checked
    const checkbox = screen.getByTestId("allowlist-checkbox-openai/gpt-4o") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
  });

  it("edit mode with existing allowlist + no re-test shows retest hint", async () => {
    const existingConfig = {
      ...OPENROUTER_CONFIG,
      allowed_models: ["openai/gpt-4o", "google/gemini-flash"],
    };
    mockProvidersState.providers = [existingConfig];

    const Section = await freshSection();
    await openEditForm(Section);

    // No test fired — picker should be absent, retest hint should be visible
    expect(screen.queryByTestId("allowlist-picker")).toBeNull();
    await waitFor(() => {
      expect(screen.getByTestId("allowlist-retest-hint")).toBeTruthy();
    });

    expect(screen.getByTestId("allowlist-retest-hint").textContent).toContain("2 models");
    expect(screen.getByTestId("allowlist-retest-hint").textContent).toContain("Test connection");
  });

  it("empty filter shows no-matches message", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockTestSuccess(["openai/gpt-4o"]);

    const Section = await freshSection();
    await openEditForm(Section);
    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-filter")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("allowlist-filter"), { target: { value: "zzz-no-match" } });

    await waitFor(() => {
      expect(screen.getByTestId("allowlist-list").textContent).toContain("No models match");
    });
  });
});
