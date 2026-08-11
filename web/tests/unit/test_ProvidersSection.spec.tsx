/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for ProvidersSection (Settings → Providers).
 *
 * Locked behaviours:
 *   - Provider list renders slug, base_url, api_key_set badge, reachability.
 *   - "Add provider" button opens the form; saving calls upsertProvider.mutate.
 *   - api_key is NEVER displayed in the list — only api_key_set badge shown.
 *   - "Test connection" calls testProvider.mutate; ok=true shows model_count.
 *   - Test failure shows error message.
 *   - Delete button with window.confirm=true calls deleteProvider.mutate.
 *   - Cancel does not call deleteProvider.mutate.
 *   - On upsert success, models query key is invalidated.
 *   - Unreachable status renders "○ unreachable:" text.
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

// ─── Mock authStore — admin user by default ──────────────────────────────────

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
};

const OPENROUTER_STATUS_OK = {
  provider: "openrouter",
  reachable: true,
  error: null,
};

const OPENROUTER_STATUS_ERR = {
  provider: "openrouter",
  reachable: false,
  error: "connection refused",
};

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("ProvidersSection", () => {
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

  async function freshSection() {
    vi.resetModules();
    const mod = await import("@/components/ProvidersSection");
    return mod.ProvidersSection;
  }

  it("list render — renders provider with slug, base_url, api_key badge, reachability", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockProvidersState.status = [OPENROUTER_STATUS_OK];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("settings-providers-section")).toBeTruthy();
    });

    const row = screen.getByTestId("provider-row-openrouter");
    expect(row).toBeTruthy();
    // Label shows "OpenRouter" (from curated presets)
    expect(row.textContent).toContain("OpenRouter");
    // Base URL shown (truncated)
    expect(row.textContent).toContain("openrouter.ai");
    // Key set badge
    expect(row.textContent).toContain("Key set");
    // Reachable status
    expect(row.textContent).toContain("reachable");
    // api_key is NEVER shown in the list — only the boolean badge
    expect(row.textContent).not.toContain("sk-");
  });

  it("unreachable status rendered — shows '○ unreachable:' text with error", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockProvidersState.status = [OPENROUTER_STATUS_ERR];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    const row = screen.getByTestId("provider-row-openrouter");
    expect(row.textContent).toContain("unreachable");
    expect(row.textContent).toContain("connection refused");
  });

  it("no key set — shows 'No key' badge", async () => {
    mockProvidersState.providers = [{ ...OPENROUTER_CONFIG, api_key_set: false }];
    mockProvidersState.status = [];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });
    const row = screen.getByTestId("provider-row-openrouter");
    expect(row.textContent).toContain("No key");
  });

  it("add flow — clicking Add opens form; filling and saving calls upsertProvider.mutate", async () => {
    mockProvidersState.providers = [];
    mockProvidersState.status = [];

    // Simulate a successful save by calling the onSuccess callback
    mockUpsertMutate.mockImplementation((_vars, opts) => {
      opts?.onSuccess?.({
        provider: "openrouter",
        base_url: "https://openrouter.ai/api",
        default_model: null,
        extra_headers: null,
        enabled: true,
        api_key_set: true,
      });
    });

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("settings-providers-section")).toBeTruthy();
    });

    // Click Add provider
    fireEvent.click(screen.getByTestId("providers-add-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-form")).toBeTruthy();
    });

    // The form defaults to openrouter — base_url should be pre-filled
    const baseUrlInput = screen.getByTestId("provider-base-url") as HTMLInputElement;
    expect(baseUrlInput.value).toBe("https://openrouter.ai/api");

    // Type an API key
    const keyInput = screen.getByTestId("provider-api-key") as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: "sk-test-key" } });

    // Click Save
    fireEvent.click(screen.getByTestId("providers-save"));

    await waitFor(() => {
      expect(mockUpsertMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          provider: "openrouter",
          body: expect.objectContaining({
            base_url: "https://openrouter.ai/api",
            api_key: "sk-test-key",
            enabled: true,
          }),
        }),
        expect.any(Object),
      );
    });

    // Toast was shown on success
    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "success" }),
    );
  });

  it("test flow — test button calls testProvider.mutate; ok=true shows model_count", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockProvidersState.status = [OPENROUTER_STATUS_OK];

    mockTestMutate.mockImplementation((_vars, opts) => {
      opts?.onSuccess?.({ ok: true, model_count: 12, error: null });
    });

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    // Click the row to open edit form
    fireEvent.click(screen.getByTestId("provider-row-openrouter"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-form")).toBeTruthy();
    });

    // Click Test connection
    fireEvent.click(screen.getByTestId("providers-test"));

    expect(mockTestMutate).toHaveBeenCalledWith(
      expect.objectContaining({ provider: "openrouter" }),
      expect.any(Object),
    );

    await waitFor(() => {
      expect(screen.getByTestId("providers-test-result")).toBeTruthy();
    });

    expect(screen.getByTestId("providers-test-result").textContent).toContain("12");
  });

  it("test flow — error shows error message in banner", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockProvidersState.status = [];

    mockTestMutate.mockImplementation((_vars, opts) => {
      opts?.onSuccess?.({ ok: false, model_count: null, error: "unauthorized" });
    });

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("provider-row-openrouter"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-form")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("providers-test"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-test-result")).toBeTruthy();
    });

    expect(screen.getByTestId("providers-test-result").textContent).toContain("unauthorized");
  });

  it("delete flow — confirm=true calls deleteProvider.mutate", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockProvidersState.status = [];

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    mockDeleteMutate.mockImplementation((_slug, opts) => {
      opts?.onSuccess?.();
    });

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("provider-row-openrouter"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-delete")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("providers-delete"));

    expect(confirmSpy).toHaveBeenCalled();
    expect(mockDeleteMutate).toHaveBeenCalledWith("openrouter", expect.any(Object));

    confirmSpy.mockRestore();
  });

  it("delete flow — confirm=false (cancel) does NOT call deleteProvider.mutate", async () => {
    mockProvidersState.providers = [OPENROUTER_CONFIG];
    mockProvidersState.status = [];

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("provider-row-openrouter"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-delete")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("providers-delete"));

    expect(confirmSpy).toHaveBeenCalled();
    expect(mockDeleteMutate).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it("edit flow — saving without typing a new key omits api_key from PUT body", async () => {
    // Regression guard: when editing an existing provider (api_key_set=true) and
    // saving without entering a new key, the upsert body must NOT contain api_key
    // (not even an empty string). This prevents the BE from wiping the stored key.
    mockProvidersState.providers = [OPENROUTER_CONFIG]; // api_key_set=true
    mockProvidersState.status = [];

    mockUpsertMutate.mockImplementation((_vars, opts) => {
      opts?.onSuccess?.({
        ...OPENROUTER_CONFIG,
        default_model: "meta-llama/llama-3.3-70b",
      });
    });

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("provider-row-openrouter")).toBeTruthy();
    });

    // Open edit form
    fireEvent.click(screen.getByTestId("provider-row-openrouter"));

    await waitFor(() => {
      expect(screen.getByTestId("providers-form")).toBeTruthy();
    });

    // API key field starts empty (placeholder shows "saved — type to replace")
    const keyInput = screen.getByTestId("provider-api-key") as HTMLInputElement;
    expect(keyInput.value).toBe("");

    // Hint text must be visible in edit-mode with api_key_set=true.
    expect(screen.getByTestId("provider-api-key-hint").textContent).toContain(
      "Leave blank to keep the current key",
    );

    // Change only default_model — do NOT touch the key field.
    fireEvent.change(screen.getByTestId("provider-default-model"), {
      target: { value: "meta-llama/llama-3.3-70b" },
    });

    // Save
    fireEvent.click(screen.getByTestId("providers-save"));

    await waitFor(() => {
      expect(mockUpsertMutate).toHaveBeenCalled();
    });

    const [callArgs] = mockUpsertMutate.mock.calls as [
      [{ provider: string; body: Record<string, unknown> }, unknown],
    ];
    const body = callArgs[0].body;

    // api_key must be absent (undefined) — never sent as "" or null.
    expect(body).not.toHaveProperty("api_key");
    expect(body.default_model).toBe("meta-llama/llama-3.3-70b");
  });

  it("api key never displayed in the list view", async () => {
    mockProvidersState.providers = [{ ...OPENROUTER_CONFIG, api_key_set: true }];
    mockProvidersState.status = [];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("providers-list")).toBeTruthy();
    });

    const listEl = screen.getByTestId("providers-list");
    // The list should not contain any characters that look like an API key
    expect(listEl.textContent).not.toMatch(/sk-[A-Za-z0-9]+/);
    // Only the badge text should appear
    expect(listEl.textContent).toContain("Key set");
  });
});
