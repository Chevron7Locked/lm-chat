/**
 * Unit tests for the Settings sidebar-nav shell (S.1 redesign).
 *
 * Covers:
 *  - All 8 nav items render (Account / Security / Appearance / Memory /
 *    Quota / LM Studio / Integrations / Developer)
 *  - The default item when :tab is omitted is Account
 *  - Clicking an item navigates to /settings/:id
 *  - Direct visit to /settings/memory activates Memory
 *  - Falls back to Account on unknown :tab value
 *  - ArrowDown on the nav cycles forward; wraps at end
 *  - ArrowUp cycles back; wraps at start
 *  - Home jumps to first item; End jumps to last
 *  - Escape blurs the nav element
 *  - aria-selected="true" is set on the active item (tablist/tab/tabpanel pattern)
 *  - data-testid="settings-tab-{id}" is present on all nav buttons
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// ─── Mock useViewport (always desktop for unit tests) ────────────────────────

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: false }),
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

// Settings now renders the chat <Sidebar> (AppShell pattern). The sidebar
// pulls TanStack Query + router data this Settings-nav test doesn't provide,
// so mock it out — this suite verifies the Settings tablist, not the chat
// sidebar.
vi.mock("@/components/Sidebar", () => ({
  Sidebar: () =>
    createElement("div", { "data-testid": "mock-sidebar" }, "Sidebar"),
}));

// ─── Mock useAuthStore — admin user so admin-only tabs (LM Studio) render ────
// Settings filters the LM Studio tab by is_admin. These tests assert all 10
// tabs are present including LM Studio, so the rendered auth context must be
// an admin. Non-admin gating can be covered in a dedicated test.

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

  describe("Settings sidebar-nav shell", () => {
      it("renders all 12 nav items", async () => {
        await renderSettings("/settings");
        expect(screen.getByTestId("settings-tab-profile")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-login-security")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-lm-studio")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-providers")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-preset-models")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-memory-settings")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-mcp-servers")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-integrations")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-appearance")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-chat")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-quota")).toBeTruthy();
        expect(screen.getByTestId("settings-tab-developer")).toBeTruthy();
      });

  it("defaults to the Profile item when :tab is omitted", async () => {
    await renderSettings("/settings");
    const btn = screen.getByTestId("settings-tab-profile");
    expect(btn.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("mock-profile")).toBeTruthy();
  });

    it("activates the Memory item on direct visit", async () => {
      await renderSettings("/settings/memory-settings");
      const btn = screen.getByTestId("settings-tab-memory-settings");
      expect(btn.getAttribute("aria-selected")).toBe("true");
      expect(screen.getByTestId("mock-memory-settings")).toBeTruthy();
    });

  it("activates the LM Studio item on direct visit", async () => {
    await renderSettings("/settings/lm-studio");
    const btn = screen.getByTestId("settings-tab-lm-studio");
    expect(btn.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("mock-lmstudio")).toBeTruthy();
  });

  it("falls back to Profile on an unknown :tab value", async () => {
    await renderSettings("/settings/unknown");
    const btn = screen.getByTestId("settings-tab-profile");
    expect(btn.getAttribute("aria-selected")).toBe("true");
  });

    it("clicking a nav item navigates to /settings/:id", async () => {
      await renderSettings("/settings");
      const loginSecurityBtn = screen.getByTestId("settings-tab-login-security");
      fireEvent.click(loginSecurityBtn);
      const selected = screen.getByTestId("settings-tab-login-security");
      expect(selected.getAttribute("aria-selected")).toBe("true");
      expect(screen.getByTestId("mock-login-security")).toBeTruthy();
    });

    it("ArrowDown on the nav cycles to the next item", async () => {
      await renderSettings("/settings");
      const nav = screen.getByRole("tablist", { name: "Settings navigation" });
      // Profile is active (index 0), ArrowDown should go to Login & Security (index 1)
      fireEvent.keyDown(nav, { key: "ArrowDown" });
      const loginSecurityBtn = screen.getByTestId("settings-tab-login-security");
      expect(loginSecurityBtn.getAttribute("aria-selected")).toBe("true");
    });

  it("ArrowDown from the last item wraps to the first", async () => {
    await renderSettings("/settings/developer");
    const nav = screen.getByRole("tablist", { name: "Settings navigation" });
    fireEvent.keyDown(nav, { key: "ArrowDown" });
    const profileBtn = screen.getByTestId("settings-tab-profile");
    expect(profileBtn.getAttribute("aria-selected")).toBe("true");
  });

  it("ArrowUp from the first item wraps to the last", async () => {
    await renderSettings("/settings");
    const nav = screen.getByRole("tablist", { name: "Settings navigation" });
    fireEvent.keyDown(nav, { key: "ArrowUp" });
    const developerBtn = screen.getByTestId("settings-tab-developer");
    expect(developerBtn.getAttribute("aria-selected")).toBe("true");
  });

    it("Home key jumps to the first item", async () => {
      await renderSettings("/settings/memory-settings");
      const nav = screen.getByRole("tablist", { name: "Settings navigation" });
      fireEvent.keyDown(nav, { key: "Home" });
      const profileBtn = screen.getByTestId("settings-tab-profile");
      expect(profileBtn.getAttribute("aria-selected")).toBe("true");
    });

  it("End key jumps to the last item", async () => {
    await renderSettings("/settings");
    const nav = screen.getByRole("tablist", { name: "Settings navigation" });
    fireEvent.keyDown(nav, { key: "End" });
    const developerBtn = screen.getByTestId("settings-tab-developer");
    expect(developerBtn.getAttribute("aria-selected")).toBe("true");
  });

      it("data-testid is present on all nav buttons", async () => {
        await renderSettings("/settings");
        const ids = ["login-security", "lm-studio", "providers", "preset-models", "memory-settings", "mcp-servers", "integrations", "appearance", "chat", "quota", "developer"];
        for (const id of ids) {
          expect(screen.getByTestId(`settings-tab-${id}`)).toBeTruthy();
        }
      });

    it("inactive items have aria-selected=false (tablist pattern)", async () => {
      // Per WAI-ARIA, every role=tab carries aria-selected; inactive tabs
      // are "false", not absent. (Was "null" under the old aria-current pattern.)
      await renderSettings("/settings");
      const loginSecurityBtn = screen.getByTestId("settings-tab-login-security");
      expect(loginSecurityBtn.getAttribute("aria-selected")).toBe("false");
    });

    it("non-admin users do not see admin-gated nav items", async () => {
      // Re-import with non-admin auth context
      vi.doMock("@/stores/authStore", () => {
        const state = { user: { is_admin: false } as { is_admin: boolean } | null, isInitializing: false };
        return {
          useAuthStore: (selector?: (s: typeof state) => unknown) =>
            selector !== undefined ? selector(state) : state,
        };
      });

      // Reset modules so Settings re-imports authStore
      vi.resetModules();

      // Re-import Settings with the non-admin mock
      const { default: Settings } = await import("@/pages/Settings");
      const result = render(
        createElement(
          MemoryRouter,
          { initialEntries: ["/settings"] },
          createElement(
            Routes,
            null,
            createElement(Route, { path: "/settings", element: createElement(Settings) }),
            createElement(Route, { path: "/settings/:tab", element: createElement(Settings) }),
          ),
        ),
      );

      // Admin-gated items (backend routes are require_admin) should NOT be present
      expect(screen.queryByTestId("settings-tab-lm-studio")).toBeNull();
      expect(screen.queryByTestId("settings-tab-providers")).toBeNull();
      expect(screen.queryByTestId("settings-tab-preset-models")).toBeNull();
      expect(screen.queryByTestId("settings-tab-mcp-servers")).toBeNull();

      // Non-admin items should still be present
      expect(screen.getByTestId("settings-tab-profile")).toBeTruthy();
      expect(screen.getByTestId("settings-tab-login-security")).toBeTruthy();
      // Integrations is a read-only catalogue view (GET is require_user), so
      // it stays visible to non-admins (they see the list + a "contact admin" note).
      expect(screen.getByTestId("settings-tab-integrations")).toBeTruthy();
      expect(screen.getByTestId("settings-tab-memory-settings")).toBeTruthy();
      expect(screen.getByTestId("settings-tab-appearance")).toBeTruthy();
      expect(screen.getByTestId("settings-tab-chat")).toBeTruthy();
      expect(screen.getByTestId("settings-tab-quota")).toBeTruthy();
      expect(screen.getByTestId("settings-tab-developer")).toBeTruthy();

      result.unmount();
    });
  });
