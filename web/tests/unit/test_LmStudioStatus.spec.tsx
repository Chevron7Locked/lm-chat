/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for useLmStudioStatus and LmStudioStatusBadge — P13e (F-05).
 *
 * Locked behaviours:
 *   - "ok"    when /api/models returns at least one loaded model and the
 *             last successful probe is younger than `staleAfterMs`.
 *   - "error" when the query is in an error state OR zero models are loaded.
 *   - "stale" when the last successful probe is older than `staleAfterMs`.
 *   - The badge renders the visible label + dot and exposes a status data
 *     attribute for e2e assertions.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();

/** A health response indicating LM Studio is reachable with 1 loaded model. */
const HEALTHY = {
  reachable: true,
  loaded_count: 1,
  auth_failed: false,
  last_probe_at: Date.now() / 1000,
};

/**
 * Route-aware mock: /api/lmstudio/health always returns HEALTHY unless
 * the test overrides mockRequest behaviour for a specific URL.  All
 * other requests (e.g. /api/models, /api/settings/lmstudio) fall
 * through to whatever the test set via mockRequest.mockResolvedValue().
 */
let healthOverride: unknown = null;
function routedRequest(url: string, ..._rest: unknown[]) {
  if (url === "/api/lmstudio/health") {
    return Promise.resolve(healthOverride ?? HEALTHY);
  }
  if (url === "/api/settings/lmstudio") {
    return Promise.resolve({ auth_failed: false, key_pruned: false });
  }
  return mockRequest(url, ..._rest);
}

vi.mock("@/lib/api", () => ({
  api: { request: (url: string, ...rest: unknown[]) => routedRequest(url, ...rest), postForm: vi.fn() },
  ApiClient: vi.fn(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      user: { id: 1, username: "test", is_admin: false },
      isInitializing: false,
      isLoading: false,
      error: null,
    };
    if (typeof selector === "function") return selector(state);
    return state;
  },
}));

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

describe("useLmStudioStatus", () => {
  beforeEach(() => { vi.clearAllMocks(); healthOverride = null; });

  it("returns 'ok' when at least one loaded model is returned within the staleness window", async () => {
    mockRequest.mockResolvedValue([
      {
        key: "qwen3",
        display_name: "Qwen 3.6B",
        capabilities: {},
        loaded_instances: 1,
        loaded_instance_ids: ["i1"],
        size_bytes: 0,
        params_string: "",
        quantization: null,
      },
    ]);
    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });
    await waitFor(() => { expect(result.current.status).toBe("ok"); });
    expect(result.current.tooltip.toLowerCase()).toContain("connected");
  });

  it("returns 'error' when /api/models rejects", async () => {
    mockRequest.mockRejectedValue(new Error("network"));
    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });
    await waitFor(() => { expect(result.current.status).toBe("error"); });
    expect(result.current.tooltip.toLowerCase()).toContain("probe");
  });

  it("returns 'error' when models load but none are loaded", async () => {
    // The "0 models loaded" RED decision now reads the LIVE health signal
    // (health.loaded_count), not the stale catalog — so drive it via health.
    healthOverride = {
      reachable: true,
      loaded_count: 0,
      auth_failed: false,
      last_probe_at: null,
    };
    mockRequest.mockResolvedValue([
      {
        key: "qwen3",
        display_name: "Qwen 3.6B",
        capabilities: {},
        loaded_instances: 0,
        loaded_instance_ids: [],
        size_bytes: 0,
        params_string: "",
        quantization: null,
      },
    ]);
    const { useLmStudioStatus } = await import("@/hooks/useLmStudioStatus");
    const { result } = renderHook(() => useLmStudioStatus(), { wrapper: makeWrapper() });
    await waitFor(() => { expect(result.current.status).toBe("error"); });
    expect(result.current.tooltip.toLowerCase()).toContain("no models loaded");
  });
});

describe("LmStudioStatusBadge", () => {
  beforeEach(() => { vi.clearAllMocks(); healthOverride = null; });

  it("renders the label and a status-keyed data attribute", async () => {
    mockRequest.mockResolvedValue([
      {
        key: "qwen3",
        display_name: "Qwen 3.6B",
        capabilities: {},
        loaded_instances: 1,
        loaded_instance_ids: ["i1"],
        size_bytes: 0,
        params_string: "",
        quantization: null,
      },
    ]);
    const { LmStudioStatusBadge } = await import("@/components/LmStudioStatusBadge");
    const wrapper = makeWrapper();
    render(createElement(wrapper, null, createElement(LmStudioStatusBadge)));

    const badge = await screen.findByTestId("lm-studio-status-badge");
    await waitFor(() => {
      expect(badge.getAttribute("data-status")).toBe("ok");
    });
    expect(badge.textContent).toContain("LM Studio");
  });

  it("renders in compact mode without the visible label text", async () => {
    mockRequest.mockResolvedValue([
      {
        key: "qwen3",
        display_name: "Qwen 3.6B",
        capabilities: {},
        loaded_instances: 1,
        loaded_instance_ids: ["i1"],
        size_bytes: 0,
        params_string: "",
        quantization: null,
      },
    ]);
    const { LmStudioStatusBadge } = await import("@/components/LmStudioStatusBadge");
    const wrapper = makeWrapper();
    render(
      createElement(wrapper, null, createElement(LmStudioStatusBadge, { compact: true }))
    );
    const badge = await screen.findByTestId("lm-studio-status-badge");
    // The aria-label still describes the connection, but the visible label is hidden.
    expect(badge.textContent.includes("LM Studio")).toBe(false);
  });
});
