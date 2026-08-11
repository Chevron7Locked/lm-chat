/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Settings page unit tests — page-level smoke.
 *
 * Complements test_Settings_tabs.spec.tsx (which covers the keyboard
 * roving-tabindex + nav contract with all sections mocked) by:
 *   - asserting the page renders with default Account tab visible,
 *   - asserting clicking each tab swaps the rendered content,
 *   - covering the AppearanceSection's theme toggle (calls themeStore.setTheme),
 *   - covering the AppearanceSection's model-select dropdown render.
 *
 * Sidebar, AccountSection, SecuritySection, MemorySection, QuotaSection,
 * LmStudioSection, IntegrationsSection, DeveloperSection are mocked so the
 * test stays focused on the page shell + AppearanceSection contract.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// ─── Mock useViewport (always desktop) ───────────────────────────────────────

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: false }),
}));

// ─── Mock all non-Appearance sections so the page shell stays focused ───────

vi.mock("@/components/LoginSecuritySection", () => ({
  LoginSecuritySection: () =>
    createElement("div", { "data-testid": "mock-login-security" }, "Login & Security section"),
}));
vi.mock("@/components/MemorySettings", () => ({
  MemorySettings: () =>
    createElement("div", { "data-testid": "mock-memory-settings" }, "Memory settings"),
}));
vi.mock("@/components/ProvidersSection", () => ({
  ProvidersSection: () =>
    createElement("div", { "data-testid": "mock-providers" }, "Providers section"),
}));
vi.mock("@/components/PresetModelsSection", () => ({
  PresetModelsSection: () =>
    createElement("div", { "data-testid": "mock-preset-models" }, "Preset models section"),
}));
vi.mock("@/components/McpStoreSection", () => ({
  McpStoreSection: () =>
    createElement("div", { "data-testid": "mock-mcp-servers" }, "MCP servers section"),
}));
vi.mock("@/components/QuotaSection", () => ({
  QuotaSection: () =>
    createElement("div", { "data-testid": "mock-quota" }, "Quota section"),
}));
vi.mock("@/components/LmStudioSection", () => ({
  LmStudioSection: () =>
    createElement("div", { "data-testid": "mock-lmstudio" }, "LM Studio section"),
}));
vi.mock("@/components/IntegrationsSection", () => ({
  IntegrationsSection: () =>
    createElement("div", { "data-testid": "mock-integrations" }, "Integrations section"),
}));
vi.mock("@/components/DeveloperSection", () => ({
  DeveloperSection: () =>
    createElement("div", { "data-testid": "mock-developer" }, "Developer section"),
}));
vi.mock("@/components/ProfileSection", () => ({
  ProfileSection: () =>
    createElement("div", { "data-testid": "mock-profile" }, "Profile section"),
}));
vi.mock("@/components/ChatSection", () => ({
  ChatSection: () =>
    createElement("div", { "data-testid": "mock-chat-settings" }, "Chat section"),
}));

// ─── Mock Sidebar (Settings renders the chat sidebar) ────────────────────────

vi.mock("@/components/Sidebar", () => ({
  Sidebar: () =>
    createElement("div", { "data-testid": "mock-sidebar" }, "Sidebar"),
}));

// ─── Mock useAuthStore — admin user so admin-only tabs (LM Studio) render ────

vi.mock("@/stores/authStore", () => {
  const state = { user: { is_admin: true } as { is_admin: boolean } | null, isInitializing: false };
  return {
    useAuthStore: (selector?: (s: typeof state) => unknown) =>
      selector !== undefined ? selector(state) : state,
  };
});

// ─── Mock themeStore so AppearanceSection's theme buttons can be exercised ──

const mockSetTheme = vi.fn();
vi.mock("@/stores/themeStore", () => ({
  useThemeStore: () => ({
    theme: "dark",
    setTheme: (t: string, anchor?: { x: number; y: number }) => mockSetTheme(t, anchor),
  }),
}));

// ─── Mock useModels for the default-model dropdown ──────────────────────────

const mockUseModels = vi.fn();
vi.mock("@/hooks/useModels", () => ({
  useModels: () => mockUseModels(),
}));

async function renderSettings(initialPath: string) {
  const { default: Settings } = await import("@/pages/Settings");
  return render(
    createElement(
      MemoryRouter,
      { initialEntries: [initialPath] },
      createElement(
        Routes,
        null,
        createElement(Route, { path: "/settings", element: createElement(Settings) }),
        createElement(Route, { path: "/settings/:tab", element: createElement(Settings) }),
      ),
    ),
  );
}

describe("Settings page", () => {
  beforeEach(() => {
    vi.resetModules();
    mockSetTheme.mockReset();
    mockUseModels.mockReset();
    mockUseModels.mockReturnValue({
      data: { models: [{ id: "gpt-foo", name: "GPT Foo", loaded: true }] },
      isLoading: false,
    });
    cleanup();
  });

  it("renders the page shell with Profile as the default active tab", async () => {
    await renderSettings("/settings");
    expect(screen.getByTestId("settings-page")).toBeTruthy();
    expect(screen.getByTestId("settings-tab-profile").getAttribute("aria-selected")).toBe(
      "true",
    );
    expect(screen.getByTestId("mock-profile")).toBeTruthy();
  });

    it("clicking each tab swaps the rendered content pane", async () => {
      await renderSettings("/settings");

      // Profile → Login & Security
      fireEvent.click(screen.getByTestId("settings-tab-login-security"));
      expect(screen.getByTestId("mock-login-security")).toBeTruthy();
      expect(screen.queryByTestId("mock-profile")).toBeNull();

      // Login & Security → Memory
      fireEvent.click(screen.getByTestId("settings-tab-memory-settings"));
      expect(screen.getByTestId("mock-memory-settings")).toBeTruthy();
      expect(screen.queryByTestId("mock-login-security")).toBeNull();

      // Memory → LM Studio
      fireEvent.click(screen.getByTestId("settings-tab-lm-studio"));
      expect(screen.getByTestId("mock-lmstudio")).toBeTruthy();
    });

  it("the Appearance tab renders the theme toggle", async () => {
    await renderSettings("/settings/appearance");
    expect(screen.getByTestId("settings-appearance-section")).toBeTruthy();

    // Theme buttons: Dark / Light / System.
    expect(screen.getByRole("button", { name: "Dark" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Light" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "System" })).toBeTruthy();
    // The Default-model select moved to Settings → Chat (Area 10
    // refactor) — Appearance now carries true visual prefs only.
  });

  it("clicking a theme button calls themeStore.setTheme", async () => {
    await renderSettings("/settings/appearance");
    fireEvent.click(screen.getByRole("button", { name: "Light" }));
    expect(mockSetTheme).toHaveBeenCalledWith("light", expect.any(Object));
  });
});
