/**
 * Unit tests for the Settings mobile nav — native <select> regression net.
 *
 * A previous mobile implementation rendered a look-alike trigger button
 * (styled like a dropdown) that opened a full-height slide-in panel with a
 * backdrop scrim — same drawer grammar as the app's Sidebar. That read as
 * "a sidebar opened", not "a dropdown dropped down". The fix replaced it
 * with a real <select> (one <optgroup> per nav group) so tapping it opens
 * the platform's own dropdown/picker.
 *
 * This file is the sibling of test_Settings_tabs.spec.tsx, which hard-mocks
 * `isMobile: false` for every test — it never exercises the mobile branch.
 * This file mocks `isMobile: true` instead, so the two suites are mutually
 * exclusive by construction and neither needs per-test viewport overrides.
 *
 * Covers:
 *  - The native <select data-testid="settings-nav-select"> renders with one
 *    <optgroup> per nav group (Account/Models/Memory/Tools/Preferences) and
 *    all 12 section <option>s.
 *  - Changing the select navigates to the chosen section and swaps the
 *    rendered content pane.
 *  - The retired slide-in panel is gone: no trigger button, no panel/backdrop
 *    classes, no desktop tablist — red-on-revert if the button+panel comes
 *    back.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// ─── Mock useViewport (always mobile for this file) ──────────────────────────

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: true }),
}));

// ─── Mocks for child sections (keep the test focused on the shell) ──────────

vi.mock("@/components/AppearanceSection", () => ({
  AppearanceSection: () =>
    createElement("div", { "data-testid": "mock-appearance" }, "Appearance section"),
}));
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

// Settings' rendered tree, indirectly, can reach the chat <Sidebar> module
// graph; mocked out for the same reason test_Settings_tabs.spec.tsx mocks it
// — this suite verifies the Settings mobile nav, not the chat sidebar.
vi.mock("@/components/Sidebar", () => ({
  Sidebar: () =>
    createElement("div", { "data-testid": "mock-sidebar" }, "Sidebar"),
}));

// ─── Mock useAuthStore — admin user so admin-only sections render ───────────
// Same rationale as test_Settings_tabs.spec.tsx: admin:true surfaces all 12
// items across all 5 groups so the optgroup/option assertions below are
// exhaustive. Non-admin gating is already covered there; not re-tested here.

vi.mock("@/stores/authStore", () => {
  const state = { user: { is_admin: true } as { is_admin: boolean } | null, isInitializing: false };
  return {
    useAuthStore: (selector?: (s: typeof state) => unknown) =>
      selector !== undefined ? selector(state) : state,
  };
});

beforeEach(() => { vi.clearAllMocks(); });

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

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("Settings mobile nav — native <select>", () => {
  it("renders a native <select> with 5 optgroups and all 12 section options", async () => {
    await renderSettings("/settings");

    const select = screen.getByTestId("settings-nav-select");
    expect(select.tagName).toBe("SELECT");

    const optgroups = select.querySelectorAll("optgroup");
    expect(optgroups.length).toBe(5);
    expect(Array.from(optgroups).map((g) => g.getAttribute("label"))).toEqual([
      "Account",
      "Models",
      "Memory",
      "Tools",
      "Preferences",
    ]);

    const options = select.querySelectorAll("option");
    expect(Array.from(options).map((o) => o.textContent)).toEqual([
      "Profile",
      "Login & Security",
      "LM Studio",
      "Providers",
      "Preset models",
      "Memory",
      "MCP Servers",
      "Integrations",
      "Appearance",
      "Chat",
      "Quota",
      "Developer",
    ]);
  });

  it("defaults to Profile selected and rendered", async () => {
    await renderSettings("/settings");
    const select = screen.getByTestId("settings-nav-select") as HTMLSelectElement;
    expect(select.value).toBe("profile");
    expect(screen.getByTestId("mock-profile")).toBeTruthy();
  });

  it("changing the select navigates to the chosen section and swaps content", async () => {
    await renderSettings("/settings");
    const select = screen.getByTestId("settings-nav-select") as HTMLSelectElement;

    fireEvent.change(select, { target: { value: "login-security" } });

    expect(select.value).toBe("login-security");
    expect(screen.getByTestId("mock-login-security")).toBeTruthy();
    expect(screen.queryByTestId("mock-profile")).toBeNull();
  });

  it("activates the section from a direct /settings/:tab visit", async () => {
    await renderSettings("/settings/memory-settings");
    const select = screen.getByTestId("settings-nav-select") as HTMLSelectElement;
    expect(select.value).toBe("memory-settings");
    expect(screen.getByTestId("mock-memory-settings")).toBeTruthy();
  });

  // ── Regression net: the retired button+slide-in-panel must stay retired ──

  it("does NOT render the old mobile trigger button", async () => {
    await renderSettings("/settings");
    expect(screen.queryByTestId("settings-nav-trigger")).toBeNull();
  });

  it("does NOT render the old panel-close button or panel/backdrop chrome", async () => {
    const { container } = await renderSettings("/settings");
    expect(screen.queryByTestId("settings-nav-close")).toBeNull();
    expect(container.querySelector(".lmchat-settings-nav--panel")).toBeNull();
    expect(container.querySelector(".lmchat-settings-nav--open")).toBeNull();
    expect(container.querySelector(".lmchat-mobile-backdrop")).toBeNull();
    expect(container.querySelector(".lmchat-settings-nav-backdrop")).toBeNull();
  });

  it("does NOT render the desktop tablist alongside the mobile select", async () => {
    await renderSettings("/settings");
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByTestId("settings-tab-profile")).toBeNull();
  });
});
