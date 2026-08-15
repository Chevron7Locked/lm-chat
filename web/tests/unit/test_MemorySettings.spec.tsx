/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for MemorySettings (Settings → Memory tab).
 *
 * Covers:
 *   - Renders the section container with loading/error states
 *   - Master toggle renders and sends PATCH on change
 *   - Sub-session toggle is disabled when master toggle is off
 *   - Web search provider select renders and sends PATCH on change
 *   - SearXNG URL input only shown when provider is "searxng"
 *   - Repeat-loop cut (K) edit/save/cancel flow, empty-clears-to-default
 *   - Non-admin users see disabled toggles (no PATCH fires)
 *   - MemoryIndexingCard is rendered
 *   - Override badges show "(default)" vs "(override)"
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";

// ─── Mock api.request ────────────────────────────────────────────────────────

const mockRequest = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: unknown[]) => mockRequest(...args) as Promise<unknown>,
    postForm: vi.fn(),
  },
  ApiClient: vi.fn(),
}));

// ─── Mock authStore ──────────────────────────────────────────────────────────

let isAdminUser = true;

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { is_admin: isAdminUser } as { is_admin: boolean } | null,
      isInitializing: false,
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

// ─── Mock MemoryIndexingCard ─────────────────────────────────────────────────

vi.mock("@/components/LmStudioSection", () => ({
  MemoryIndexingCard: () =>
    createElement("div", { "data-testid": "mock-memory-indexing-card" }, "Indexing status"),
}));

// ─── Mock useLmStudioConfig ──────────────────────────────────────────────────

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({
    data: {
      base_url: "http://localhost:1234",
      default_model: "gpt-4",
      api_key_set: true,
      source_base_url: "user",
      source_api_key: "unset",
      source_default_model: "env",
      loaded_embedding_models: [],
      loaded_background_models: [],
    },
    isLoading: false,
  }),
  lmStudioConfigKeys: {
    all: ["lmstudio-config"],
    resolved: () => ["lmstudio-config", "resolved"],
  },
}));

// ─── Import component AFTER mocks (vitest hoists mocks) ──────────────────────

import { MemorySettings } from "@/components/MemorySettings";

// ─── Helpers ──────────────────────────────────────────────────────────────────

const DEFAULT_SETTINGS = {
  memory_distillation_enabled: { value: true, is_override: false },
  subsession_memory_distillation_enabled: { value: false, is_override: false },
  web_search_provider: { value: "ddg", is_override: false },
  searxng_url: { value: null, is_override: false },
  repeat_warning_cut_k: { value: 16, is_override: false },
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      {node}
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  isAdminUser = true;
  cleanup();
});

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("MemorySettings", () => {
  it("renders the section container with loading state", async () => {
    // Make the GET request never resolve
    mockRequest.mockImplementation(
      async (url: string) => {
        if (url === "/api/settings/app") {
          return new Promise<never>(() => {});
        }
        return Promise.reject(new Error("unexpected " + url));
      },
    );

    wrap(<MemorySettings />);

    await waitFor(() => {
      expect(screen.getByText("Loading memory settings…")).toBeTruthy();
    });
  });

  it("renders the section container with error state", async () => {
    mockRequest.mockImplementation(async (url: string) => {
      if (url === "/api/settings/app") {
        return Promise.reject(new Error("network error"));
      }
      return Promise.reject(new Error("unexpected " + url));
    });

    wrap(<MemorySettings />);

    await waitFor(() => {
      expect(screen.getByTestId("settings-memory-error")).toBeTruthy();
    });
  });

  it("renders all controls with correct initial values", async () => {
    mockRequest.mockResolvedValue(DEFAULT_SETTINGS);

    wrap(<MemorySettings />);

    // Wait for data to load
    await waitFor(() => {
      expect(screen.getByTestId("settings-memory-master-toggle")).toBeTruthy();
    });

    // Master toggle
    const masterToggle = screen.getByTestId("settings-memory-master-toggle");
    expect((masterToggle as HTMLInputElement).checked).toBe(true);

    // Sub-session toggle
    const subToggle = screen.getByTestId("settings-memory-subsession-toggle");
    expect((subToggle as HTMLInputElement).checked).toBe(false);

    // Web search provider select
    const providerSelect = screen.getByTestId("settings-memory-search-provider");
    expect((providerSelect as HTMLSelectElement).value).toBe("ddg");

    // SearXNG URL input should NOT be visible (provider is ddg)
    expect(screen.queryByTestId("settings-memory-searxng-url-input")).toBeNull();

    // Indexing card
    expect(screen.getByTestId("mock-memory-indexing-card")).toBeTruthy();
  });

  it("sends PATCH on master toggle change", async () => {
    const updatedSettings = {
      ...DEFAULT_SETTINGS,
      memory_distillation_enabled: { value: false, is_override: true },
    };

    mockRequest.mockImplementation(async (url: string, opts?: { method?: string; body?: string }) => {
      if (url === "/api/settings/app") {
        if (opts?.method === "PATCH") {
          const body = JSON.parse(opts.body ?? "{}");
          if (body.memory_distillation_enabled === false) {
            return updatedSettings;
          }
        }
        return DEFAULT_SETTINGS;
      }
      return Promise.reject(new Error("unexpected " + url));
    });

    wrap(<MemorySettings />);

    // Wait for initial data
    await waitFor(() => {
      expect(screen.getByTestId("settings-memory-master-toggle")).toBeTruthy();
    });

    // Uncheck the master toggle
    const masterToggle = screen.getByTestId("settings-memory-master-toggle");
    fireEvent.click(masterToggle);

    // PATCH should be called with the new value
    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith(
        "/api/settings/app",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ memory_distillation_enabled: false }),
        }),
      );
    });
  });

    it("sub-session toggle is disabled when master toggle is off", async () => {
      const settingsWithOffMaster = {
        ...DEFAULT_SETTINGS,
        memory_distillation_enabled: { value: false, is_override: false },
      };

      mockRequest.mockResolvedValue(settingsWithOffMaster);

      wrap(<MemorySettings />);

      // Wait for data to load
      await waitFor(() => {
        expect(screen.getByTestId("settings-memory-subsession-toggle")).toBeTruthy();
      });

      const subToggle = screen.getByTestId("settings-memory-subsession-toggle");
      expect((subToggle as HTMLInputElement).disabled).toBe(true);

      // Hint text should be visible (admin master-off hint, distinct from non-admin)
      expect(screen.getByTestId("settings-memory-subsession-hint")).toBeTruthy();
      expect(screen.getByText("Turn on automatic memory first.")).toBeTruthy();
    });

  it("sub-session toggle sends PATCH on change when master is on", async () => {
    const updatedSettings = {
      ...DEFAULT_SETTINGS,
      subsession_memory_distillation_enabled: { value: true, is_override: true },
    };

    mockRequest.mockImplementation(async (url: string, opts?: { method?: string; body?: string }) => {
      if (url === "/api/settings/app") {
        if (opts?.method === "PATCH") {
          const body = JSON.parse(opts.body ?? "{}");
          if (body.subsession_memory_distillation_enabled !== undefined) {
            return updatedSettings;
          }
        }
        return DEFAULT_SETTINGS;
      }
      return Promise.reject(new Error("unexpected " + url));
    });

    wrap(<MemorySettings />);

    // Wait for initial data
    await waitFor(() => {
      expect(screen.getByTestId("settings-memory-subsession-toggle")).toBeTruthy();
    });

    // Check the sub-session toggle
    const subToggle = screen.getByTestId("settings-memory-subsession-toggle");
    fireEvent.click(subToggle);

    // PATCH should be called
    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith(
        "/api/settings/app",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ subsession_memory_distillation_enabled: true }),
        }),
      );
    });
  });

  it("SearXNG URL input only shown when provider is searxng", async () => {
    const settingsWithSearxng = {
      ...DEFAULT_SETTINGS,
      web_search_provider: { value: "searxng", is_override: false },
    };

    mockRequest.mockResolvedValue(settingsWithSearxng);

    wrap(<MemorySettings />);

    // Wait for initial data
    await waitFor(() => {
      expect(screen.getByTestId("settings-memory-search-provider")).toBeTruthy();
    });

    // SearXNG URL edit button should be visible
    expect(screen.getByTestId("settings-memory-searxng-url-edit")).toBeTruthy();
    // Input should NOT be visible (not in edit mode yet)
    expect(screen.queryByTestId("settings-memory-searxng-url-input")).toBeNull();

    // Click edit to enter edit mode
    fireEvent.click(screen.getByTestId("settings-memory-searxng-url-edit"));

    // Input should now be visible
    expect(screen.getByTestId("settings-memory-searxng-url-input")).toBeTruthy();

    // Cancel should hide the input
    fireEvent.click(screen.getByTestId("settings-memory-searxng-url-cancel"));
    expect(screen.queryByTestId("settings-memory-searxng-url-input")).toBeNull();
  });

  it("SearXNG URL input saves the URL on save click", async () => {
    const settingsWithSearxng = {
      ...DEFAULT_SETTINGS,
      web_search_provider: { value: "searxng", is_override: false },
      searxng_url: { value: null, is_override: false },
    };

    const updatedSettings = {
      ...settingsWithSearxng,
      searxng_url: { value: "https://searxng.example.com", is_override: true },
    };

    mockRequest.mockImplementation(async (url: string, opts?: { method?: string; body?: string }) => {
      if (url === "/api/settings/app") {
        if (opts?.method === "PATCH") {
          const body = JSON.parse(opts.body ?? "{}");
          if (body.searxng_url !== undefined) {
            return updatedSettings;
          }
        }
        return settingsWithSearxng;
      }
      return Promise.reject(new Error("unexpected " + url));
    });

    wrap(<MemorySettings />);

    await waitFor(() => {
      expect(screen.getByTestId("settings-memory-search-provider")).toBeTruthy();
    });

    // Enter edit mode
    fireEvent.click(screen.getByTestId("settings-memory-searxng-url-edit"));

    // Type a URL
    const input = screen.getByTestId("settings-memory-searxng-url-input");
    fireEvent.change(input, { target: { value: "https://searxng.example.com" } });

    // Click save
    fireEvent.click(screen.getByTestId("settings-memory-searxng-url-save"));

    // PATCH should be called with the URL
    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith(
        "/api/settings/app",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ searxng_url: "https://searxng.example.com" }),
        }),
      );
    });
  });

  it("changing provider away from searxng clears the URL override", async () => {
    const settingsWithSearxng = {
      ...DEFAULT_SETTINGS,
      web_search_provider: { value: "searxng", is_override: true },
      searxng_url: { value: "https://searxng.example.com", is_override: true },
    };

    const updatedProviderSettings = {
      ...settingsWithSearxng,
      web_search_provider: { value: "ddg", is_override: true },
    };

    const clearedUrlSettings = {
      ...updatedProviderSettings,
      searxng_url: { value: null, is_override: false },
    };

    let callCount = 0;
    mockRequest.mockImplementation(async (url: string, opts?: { method?: string; body?: string }) => {
      if (url === "/api/settings/app") {
        callCount++;
        if (opts?.method === "PATCH") {
          const body = JSON.parse(opts.body ?? "{}");
          if (body.web_search_provider === "ddg") {
            return updatedProviderSettings;
          }
          if (body.searxng_url === null) {
            return clearedUrlSettings;
          }
        }
        return settingsWithSearxng;
      }
      return Promise.reject(new Error("unexpected " + url));
    });

    wrap(<MemorySettings />);

    await waitFor(() => {
      expect(screen.getByTestId("settings-memory-search-provider")).toBeTruthy();
    });

    // Change provider from searxng to ddg
    const providerSelect = screen.getByTestId("settings-memory-search-provider");
    fireEvent.change(providerSelect, { target: { value: "ddg" } });

    // Wait for both PATCH calls (provider change + URL clear)
    await waitFor(() => {
      expect(callCount).toBeGreaterThanOrEqual(2);
    });

    // Verify the PATCH calls
    const patchCalls = mockRequest.mock.calls.filter(
      (call) => call[0] === "/api/settings/app" && call[1]?.method === "PATCH",
    );
    expect(patchCalls.length).toBeGreaterThanOrEqual(2);

    // Both indices guaranteed by the length check just above.
    const [firstPatch, secondPatch] = patchCalls;
    if (firstPatch == null || secondPatch == null) {
      throw new Error("expected at least 2 PATCH calls");
    }
    // First PATCH should set provider to ddg
    expect(firstPatch[1].body).toContain("web_search_provider");
    // Second PATCH should clear the URL
    expect(secondPatch[1].body).toContain("searxng_url");
  });

    it("repeat-loop cut (K) edit button opens the numeric input", async () => {
      mockRequest.mockResolvedValue(DEFAULT_SETTINGS);

      wrap(<MemorySettings />);

      await waitFor(() => {
        expect(screen.getByTestId("settings-memory-repeat-cut-k-edit")).toBeTruthy();
      });

      // Shows the effective value on the button before editing.
      expect(screen.getByTestId("settings-memory-repeat-cut-k-edit").textContent).toBe("16");
      expect(screen.queryByTestId("settings-memory-repeat-cut-k-input")).toBeNull();

      fireEvent.click(screen.getByTestId("settings-memory-repeat-cut-k-edit"));
      expect(screen.getByTestId("settings-memory-repeat-cut-k-input")).toBeTruthy();

      fireEvent.click(screen.getByTestId("settings-memory-repeat-cut-k-cancel"));
      expect(screen.queryByTestId("settings-memory-repeat-cut-k-input")).toBeNull();
    });

    it("repeat-loop cut (K) saves an integer override on save click", async () => {
      const updatedSettings = {
        ...DEFAULT_SETTINGS,
        repeat_warning_cut_k: { value: 8, is_override: true },
      };

      mockRequest.mockImplementation(
        async (url: string, opts?: { method?: string; body?: string }) => {
          if (url === "/api/settings/app") {
            if (opts?.method === "PATCH") {
              const body = JSON.parse(opts.body ?? "{}");
              if (body.repeat_warning_cut_k !== undefined) {
                return updatedSettings;
              }
            }
            return DEFAULT_SETTINGS;
          }
          return Promise.reject(new Error("unexpected " + url));
        },
      );

      wrap(<MemorySettings />);

      await waitFor(() => {
        expect(screen.getByTestId("settings-memory-repeat-cut-k-edit")).toBeTruthy();
      });

      fireEvent.click(screen.getByTestId("settings-memory-repeat-cut-k-edit"));
      const input = screen.getByTestId("settings-memory-repeat-cut-k-input");
      fireEvent.change(input, { target: { value: "8" } });
      fireEvent.click(screen.getByTestId("settings-memory-repeat-cut-k-save"));

      await waitFor(() => {
        expect(mockRequest).toHaveBeenCalledWith(
          "/api/settings/app",
          expect.objectContaining({
            method: "PATCH",
            body: JSON.stringify({ repeat_warning_cut_k: 8 }),
          }),
        );
      });
    });

    it("repeat-loop cut (K) saving an empty draft clears the override", async () => {
      const overridden = {
        ...DEFAULT_SETTINGS,
        repeat_warning_cut_k: { value: 8, is_override: true },
      };

      mockRequest.mockImplementation(
        async (url: string, opts?: { method?: string; body?: string }) => {
          if (url === "/api/settings/app") {
            if (opts?.method === "PATCH") {
              return DEFAULT_SETTINGS;
            }
            return overridden;
          }
          return Promise.reject(new Error("unexpected " + url));
        },
      );

      wrap(<MemorySettings />);

      await waitFor(() => {
        expect(screen.getByTestId("settings-memory-repeat-cut-k-edit").textContent).toBe("8");
      });

      fireEvent.click(screen.getByTestId("settings-memory-repeat-cut-k-edit"));
      const input = screen.getByTestId("settings-memory-repeat-cut-k-input");
      fireEvent.change(input, { target: { value: "" } });
      fireEvent.click(screen.getByTestId("settings-memory-repeat-cut-k-save"));

      await waitFor(() => {
        expect(mockRequest).toHaveBeenCalledWith(
          "/api/settings/app",
          expect.objectContaining({
            method: "PATCH",
            body: JSON.stringify({ repeat_warning_cut_k: null }),
          }),
        );
      });
    });

    it("non-admin users see read-only view (no interactive toggles)", async () => {
      isAdminUser = false;

      mockRequest.mockResolvedValue(DEFAULT_SETTINGS);

      wrap(<MemorySettings />);

      await waitFor(() => {
        // Non-admins see a read-only div (not an interactive label)
        // The master toggle label div should exist
        expect(screen.getByTestId("settings-memory-master-toggle-label")).toBeTruthy();
      });

      // Non-admins should NOT see an interactive checkbox — the toggle input
      // is replaced by a plain text read-out for non-admin users.
      // For non-admins, the input (testid settings-memory-master-toggle)
      // does not exist — only the label div does.
      expect(screen.queryByTestId("settings-memory-master-toggle")).toBeNull();

      // Clicking the label div should NOT fire PATCH
      fireEvent.click(screen.getByTestId("settings-memory-master-toggle-label"));
      const patchCalls = mockRequest.mock.calls.filter(
        (call) => call[1]?.method === "PATCH",
      );
      expect(patchCalls).toHaveLength(0);

      // Sub-session toggle: also read-only (no interactive checkbox)
      expect(screen.queryByTestId("settings-memory-subsession-toggle")).toBeNull();
      fireEvent.click(screen.getByTestId("settings-memory-subsession-toggle-label"));
      const patchCallsAfter = mockRequest.mock.calls.filter(
        (call) => call[1]?.method === "PATCH",
      );
      expect(patchCallsAfter).toHaveLength(0);

      // Override badges are still visible
      const badges = screen.getAllByTestId("settings-memory-override-badge");
      expect(badges.length).toBeGreaterThanOrEqual(2);

      // Pinned insights row should be visible
      expect(screen.getByTestId("settings-memory-admin-chip")).toBeTruthy();
      expect(screen.getByText("admin-set")).toBeTruthy();
    });

    it("override badges show (default) vs (override)", async () => {
      const settingsWithOverride = {
        ...DEFAULT_SETTINGS,
        memory_distillation_enabled: { value: true, is_override: true },
      };

      mockRequest.mockResolvedValue(settingsWithOverride);

      wrap(<MemorySettings />);

      await waitFor(() => {
        expect(screen.getByTestId("settings-memory-master-toggle")).toBeTruthy();
      });

      // Override badges should be visible (use getAllByTestId since multiple exist)
      const badges = screen.getAllByTestId("settings-memory-override-badge");
      // At least the master toggle badge should show "(override)"
      const masterBadge = badges.find((b) => b.textContent === "(override)");
      expect(masterBadge).toBeTruthy();
    });

    it("toggles use lmchat-mcp-toggle markup (not legacy lmchat-toggle__input)", async () => {
      mockRequest.mockResolvedValue(DEFAULT_SETTINGS);

      wrap(<MemorySettings />);

      await waitFor(() => {
        expect(screen.getByTestId("settings-memory-master-toggle")).toBeTruthy();
      });

      // Master toggle label should have the lmchat-mcp-toggle class
      const masterLabel = screen.getByTestId("settings-memory-master-toggle-label");
      expect(masterLabel.classList.contains("lmchat-mcp-toggle")).toBe(true);

      // Master toggle input should have the lmchat-mcp-toggle__input class
      const masterInput = masterLabel.querySelector("input");
      expect(masterInput).toBeTruthy();
      if (masterInput == null) throw new Error("expected master toggle <input> to exist");
      expect(masterInput.classList.contains("lmchat-mcp-toggle__input")).toBe(true);

      // Master toggle should have the track span
      const track = masterLabel.querySelector(".lmchat-mcp-toggle__track");
      expect(track).toBeTruthy();

      // Sub-session toggle should also use the same pattern
      const subLabel = screen.getByTestId("settings-memory-subsession-toggle-label");
      expect(subLabel.classList.contains("lmchat-mcp-toggle")).toBe(true);
      const subInput = subLabel.querySelector("input");
      expect(subInput).toBeTruthy();
      if (subInput == null) throw new Error("expected sub-session toggle <input> to exist");
      expect(subInput.classList.contains("lmchat-mcp-toggle__input")).toBe(true);
    });

    it("pinned insights row shows admin-set chip", async () => {
      mockRequest.mockResolvedValue(DEFAULT_SETTINGS);

      wrap(<MemorySettings />);

      await waitFor(() => {
        expect(screen.getByTestId("settings-memory-admin-chip")).toBeTruthy();
      });

      expect(screen.getByText("admin-set")).toBeTruthy();
      expect(screen.getByText("Up to 100 per user")).toBeTruthy();
    });
  });
