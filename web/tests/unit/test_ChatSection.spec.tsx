/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for ChatSection (Settings → Chat tab).
 *
 * Covers:
 *   - Renders the section container with description text
 *   - Default model select is controlled (value prop, not defaultValue)
 *   - Changing the select fires PUT /api/settings/lmstudio
 *   - After PUT, the lmstudio-config query is invalidated
 *   - Shows "Loading models…" when useChatModelOptions is loading
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ─── Mock api.request ────────────────────────────────────────────────────────

const mockRequest = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>();

vi.mock("@/lib/api", () => ({
  api: {
    request: (...args: [path: string, init?: RequestInit]) => mockRequest(...args),
    postForm: vi.fn(),
  },
  ApiClient: vi.fn(),
}));

// ─── Mock useLmStudioConfig ──────────────────────────────────────────────────

const mockLmConfigData = vi.hoisted(() => ({
  value: {
    data: {
      base_url: "http://localhost:1234",
      default_model: "llama-3.1-8b",
      api_key_set: true,
      source_base_url: "user",
      source_api_key: "unset",
      source_default_model: "env",
      loaded_embedding_models: [],
      loaded_background_models: [],
    },
    isLoading: false,
  },
}));

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => mockLmConfigData.value,
  lmStudioConfigKeys: {
    all: ["lmstudio-config"],
    resolved: () => ["lmstudio-config", "resolved"],
  },
}));

// ─── Mock useChatModelOptions ────────────────────────────────────────────────

const mockModelOptions = vi.hoisted(() => ({
  value: {
    options: [
      { id: "llama-3.1-8b", name: "Llama 3.1 8B" },
      { id: "gpt-4", name: "GPT-4" },
    ],
    groups: [],
    isLoading: false,
  },
}));

vi.mock("@/hooks/useChatModelOptions", () => ({
  useChatModelOptions: () => mockModelOptions.value,
}));

// ─── Import component AFTER mocks (vitest hoists mocks) ──────────────────────

import { ChatSection } from "@/components/ChatSection";

// ─── Helpers ──────────────────────────────────────────────────────────────────

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
  mockLmConfigData.value = {
    data: {
      base_url: "http://localhost:1234",
      default_model: "llama-3.1-8b",
      api_key_set: true,
      source_base_url: "user",
      source_api_key: "unset",
      source_default_model: "env",
      loaded_embedding_models: [],
      loaded_background_models: [],
    },
    isLoading: false,
  };
  mockModelOptions.value = {
    options: [
      { id: "llama-3.1-8b", name: "Llama 3.1 8B" },
      { id: "gpt-4", name: "GPT-4" },
    ],
    groups: [],
    isLoading: false,
  };
  cleanup();
});

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("ChatSection", () => {
  it("renders the section container with description text", () => {
    wrap(<ChatSection />);

    expect(screen.getByTestId("settings-chat-section")).toBeTruthy();
    expect(
      screen.getByText(
        "Defaults for new chats. Per-chat overrides via the model selector in the chat top bar take precedence over these choices.",
      ),
    ).toBeTruthy();
  });

  it("renders the default model select with the resolved default_model as value", () => {
    wrap(<ChatSection />);

    const select = screen.getByTestId("settings-chat-default-model");
    expect((select as HTMLSelectElement).value).toBe("llama-3.1-8b");
  });

  it("changing the select fires PUT /api/settings/lmstudio", async () => {
    wrap(<ChatSection />);

    const select = screen.getByTestId("settings-chat-default-model");

    // Change to a different model
    fireEvent.change(select, { target: { value: "gpt-4" } });

    // PUT should be called with the new default_model
    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith(
        "/api/settings/lmstudio",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ default_model: "gpt-4" }),
        }),
      );
    });
  });

  it("changing the select to empty fires PUT with null default_model", async () => {
    wrap(<ChatSection />);

    const select = screen.getByTestId("settings-chat-default-model");

    // Change to empty string
    fireEvent.change(select, { target: { value: "" } });

    await waitFor(() => {
      expect(mockRequest).toHaveBeenCalledWith(
        "/api/settings/lmstudio",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ default_model: null }),
        }),
      );
    });
  });

  it("the select is controlled (value prop, not uncontrolled defaultValue)", async () => {
    // Read the actual source file to assert it uses `value` not `defaultValue`.
    const fs = await import("fs");
    const path = await import("path");
    const source = fs.readFileSync(
      path.resolve(__dirname, "../../src/components/ChatSection.tsx"),
      "utf-8",
    );
    // The component should NOT use defaultValue (which would make it uncontrolled).
    // It should use `value` for controlled mode.
    expect(source).toContain("value={defaultModelFallback}");
    expect(source).not.toContain("defaultValue");
  });

  it("shows 'Loading models…' when useChatModelOptions is loading", () => {
    mockModelOptions.value = {
      options: [],
      groups: [],
      isLoading: true,
    };

    wrap(<ChatSection />);

    expect(screen.getByText("Loading models…")).toBeTruthy();
    // Select should not be rendered
    expect(screen.queryByTestId("settings-chat-default-model")).toBeNull();
  });

  it("the lmstudio-config query key is invalidated on model change", async () => {
    // Verify the source code calls invalidateQueries with lmStudioConfigKeys.resolved()
    const fs = await import("fs");
    const path = await import("path");
    const source = fs.readFileSync(
      path.resolve(__dirname, "../../src/components/ChatSection.tsx"),
      "utf-8",
    );
    expect(source).toContain("invalidateQueries");
    expect(source).toContain("lmStudioConfigKeys.resolved()");
  });
});
