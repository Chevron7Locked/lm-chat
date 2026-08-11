/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Analytics page unit tests — render + sidebar-hero trio assertion.
 *
 * Locked behaviours (PR-G + PR-J coverage):
 *   - The hero surfaces the three values useSidebarStats yields: an
 *     approxSavedUsd headline, tokensToday, and a streaming-tps row.
 *     The page and the sidebar footer share useSidebarStats so they can
 *     never disagree.
 *   - Per-user analytics rows ("total messages", "total chats",
 *     "messages in the last 7 days") render from useMyAnalytics().
 *   - The admin system block only renders when authStore says user.is_admin.
 *
 * Hooks are mocked at module level so the suite does not need a TanStack
 * Query provider; Sidebar is mocked because AppShell pulls it in.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  cleanup,
} from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import type { UserAnalytics } from "@/hooks/useAnalytics";

// Force prefers-reduced-motion=true so the hero count-up hook short-circuits
// to its terminal value and never schedules requestAnimationFrame. Otherwise
// jsdom's RAF polyfill recurses synchronously through the easing loop and
// blows the stack.
beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    (_q: string) => ({
      matches: true,
      media: _q,
      onchange: null,
      addEventListener: () => { /* noop */ },
      removeEventListener: () => { /* noop */ },
      addListener: () => { /* noop */ },
      removeListener: () => { /* noop */ },
      dispatchEvent: () => false,
    }),
  );
  // Also patch on window for code that reads window.matchMedia directly.
  if (typeof window !== "undefined") {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      configurable: true,
      value: (q: string) => ({
        matches: true,
        media: q,
        onchange: null,
        addEventListener: () => { /* noop */ },
        removeEventListener: () => { /* noop */ },
        addListener: () => { /* noop */ },
        removeListener: () => { /* noop */ },
        dispatchEvent: () => false,
      }),
    });
  }
});

// ─── Mock authStore — non-admin user by default; overridden per-case ────────

const mockUseAuthStore = vi.fn();
vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => mockUseAuthStore(),
}));

// ─── Mock analytics hooks ────────────────────────────────────────────────────

const mockUseMyAnalytics = vi.fn();
const mockUseSystemAnalytics = vi.fn();

vi.mock("@/hooks/useAnalytics", () => ({
  useMyAnalytics: () => mockUseMyAnalytics(),
  useSystemAnalytics: () => mockUseSystemAnalytics(),
}));

// ─── Mock useSidebarStats (the single source the hero pulls from) ───────────

const mockUseSidebarStats = vi.fn();

vi.mock("@/hooks/useSidebarStats", () => ({
  useSidebarStats: () => mockUseSidebarStats(),
  formatTokens: (n: number) => `${String(n)} tk`,
  formatUsd: (n: number) => `$${n.toFixed(2)}`,
}));

// ─── Mock Sidebar (AppShell pulls it in) ─────────────────────────────────────

vi.mock("@/components/Sidebar", () => ({
  Sidebar: () =>
    createElement("div", { "data-testid": "mock-sidebar" }, "Sidebar"),
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const ME_BASE: UserAnalytics = {
  total_messages: 42,
  total_chats: 7,
  messages_last_7_days: 12,
  messages_by_day: [],
  top_models: [],
};

async function freshAnalytics() {
  vi.resetModules();
  const mod = await import("@/pages/Analytics");
  return mod.default;
}

function renderAnalytics(Page: React.ComponentType) {
  return render(
    <MemoryRouter initialEntries={["/analytics"]}>
      <Routes>
        <Route path="/analytics" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Analytics", () => {
  beforeEach(() => {
    mockUseMyAnalytics.mockReset();
    mockUseSystemAnalytics.mockReset();
    mockUseSidebarStats.mockReset();
    mockUseAuthStore.mockReset();
    cleanup();
  });

  it("renders the three sidebar-sourced hero metrics", async () => {
    mockUseAuthStore.mockReturnValue({
      user: { id: 1, username: "alice", is_admin: false, totp_enabled: false },
      isInitializing: false,
    });
    mockUseMyAnalytics.mockReturnValue({
      data: ME_BASE,
      isLoading: false,
      isError: false,
    });
    mockUseSystemAnalytics.mockReturnValue({ data: undefined, isLoading: false });
    mockUseSidebarStats.mockReturnValue({
      tokensToday: 1234,
      approxSavedUsd: 12.34,
      streamingTps: 45,
      isReady: true,
    });

    const Page = await freshAnalytics();
    renderAnalytics(Page);

    // approxSavedUsd headline — formatted "$12.34", labeled by the
    // "saved vs cloud" subtitle.
    expect(screen.getByText("saved vs cloud")).toBeTruthy();
    expect(screen.getByLabelText("$12.34 saved vs cloud")).toBeTruthy();

    // tokensToday + streamingTps support rows.
    expect(screen.getByText("1234 tk")).toBeTruthy();
    expect(screen.getByText("tokens today")).toBeTruthy();
    expect(screen.getByText("45")).toBeTruthy();
    expect(screen.getByText("tokens / second · last stream")).toBeTruthy();
  });

  it("renders the per-user analytics rows from useMyAnalytics", async () => {
    mockUseAuthStore.mockReturnValue({
      user: { id: 1, username: "alice", is_admin: false, totp_enabled: false },
      isInitializing: false,
    });
    mockUseMyAnalytics.mockReturnValue({
      data: ME_BASE,
      isLoading: false,
      isError: false,
    });
    mockUseSystemAnalytics.mockReturnValue({ data: undefined, isLoading: false });
    mockUseSidebarStats.mockReturnValue({
      tokensToday: 0,
      approxSavedUsd: 0,
      streamingTps: null,
      isReady: true,
    });

    const Page = await freshAnalytics();
    renderAnalytics(Page);

    expect(screen.getByText("total messages")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getByText("total chats")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    expect(screen.getByText("messages in the last 7 days")).toBeTruthy();
  });

  it("hides the admin system section for non-admin users", async () => {
    mockUseAuthStore.mockReturnValue({
      user: { id: 1, username: "alice", is_admin: false, totp_enabled: false },
      isInitializing: false,
    });
    mockUseMyAnalytics.mockReturnValue({
      data: ME_BASE,
      isLoading: false,
      isError: false,
    });
    mockUseSystemAnalytics.mockReturnValue({ data: undefined, isLoading: false });
    mockUseSidebarStats.mockReturnValue({
      tokensToday: 100,
      approxSavedUsd: 0.05,
      streamingTps: 10,
      isReady: true,
    });

    const Page = await freshAnalytics();
    renderAnalytics(Page);

    expect(screen.queryByText("System · admin")).toBeNull();
  });

  it("shows the admin system section when user.is_admin is true", async () => {
    mockUseAuthStore.mockReturnValue({
      user: { id: 99, username: "root", is_admin: true, totp_enabled: false },
      isInitializing: false,
    });
    mockUseMyAnalytics.mockReturnValue({
      data: ME_BASE,
      isLoading: false,
      isError: false,
    });
    mockUseSystemAnalytics.mockReturnValue({
      data: {
        total_users: 4,
        total_chats: 100,
        total_messages: 1000,
        messages_last_7_days: 50,
        top_models: [],
      },
      isLoading: false,
    });
    mockUseSidebarStats.mockReturnValue({
      tokensToday: 100,
      approxSavedUsd: 0.05,
      streamingTps: 10,
      isReady: true,
    });

    const Page = await freshAnalytics();
    renderAnalytics(Page);

    expect(screen.getByText("System · admin")).toBeTruthy();
    expect(screen.getByText("total users")).toBeTruthy();
    expect(screen.getByText("4")).toBeTruthy();
  });
});
