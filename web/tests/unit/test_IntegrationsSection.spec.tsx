/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for IntegrationsSection (P13l.4).
 *
 * Locked behaviours:
 *   - Renders the read-only MCP integrations catalogue from
 *     GET /api/integrations/available.
 *   - Default-on entries get a "Default on" badge.
 *   - Admin users see a "Manage" link to /admin/integrations.
 *   - Non-admin users do NOT see the manage link; copy instead reads
 *     "Admin manages the list."
 *   - Empty list renders the empty-state message.
 *   - Loading + error states have stable testids for E2E.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function seedAuth(opts: { admin: boolean }) {
  vi.resetModules();
  const storeMod = await import("@/stores/authStore");
  storeMod.useAuthStore.setState({
    user: {
      id: opts.admin ? 2 : 1,
      username: opts.admin ? "root" : "alice",
      is_admin: opts.admin,
      totp_enabled: false,
    },
    isInitializing: false,
    isLoading: false,
    error: null,
  });
  const mod = await import("@/components/IntegrationsSection");
  return mod.IntegrationsSection;
}

function renderInRouter(Component: () => React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    createElement(
      MemoryRouter,
      { initialEntries: ["/settings/integrations"] },
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(Component),
      ),
    ),
  );
}

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("IntegrationsSection", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    cleanup();
  });

  afterEach(() => {
    // global.fetch is assigned directly (not via vi.spyOn), so vi.restoreAllMocks()
    // won't clean it up. Explicitly delete the assignment so subsequent test
    // files in the same worker don't inherit a stale fetch mock.
    // vi.resetModules() undoes any module-registry side-effects from seedAuth().
    delete (global as Record<string, unknown>).fetch;
    vi.resetModules();
    cleanup();
  });

  it("renders the catalogue with default-on badge for default entries", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: 1,
            value: "mcp/searxng",
            sort_order: 0,
            enabled_by_default: true,
            created_at: "2026-05-22T00:00:00Z",
            updated_at: "2026-05-22T00:00:00Z",
          },
          {
            id: 2,
            value: "mcp/fetch",
            sort_order: 1,
            enabled_by_default: false,
            created_at: "2026-05-22T00:00:00Z",
            updated_at: "2026-05-22T00:00:00Z",
          },
        ]),
        { status: 200 },
      ),
    );

    const Page = await seedAuth({ admin: false });
    renderInRouter(Page);

    await waitFor(() => {
      expect(screen.getByTestId("settings-integrations-list")).toBeTruthy();
    });
    expect(screen.getByTestId("settings-integrations-item-mcp/searxng")).toBeTruthy();
    expect(screen.getByTestId("settings-integrations-item-mcp/fetch")).toBeTruthy();
    // Only searxng has the default badge.
    expect(screen.getByTestId("settings-integrations-default-mcp/searxng")).toBeTruthy();
    expect(
      screen.queryByTestId("settings-integrations-default-mcp/fetch"),
    ).toBeNull();
  });

  it("renders the Manage link only for admin users", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    );

    const AdminPage = await seedAuth({ admin: true });
    renderInRouter(AdminPage);

    await waitFor(() => {
      expect(screen.getByTestId("settings-integrations-manage-link")).toBeTruthy();
    });
    const link = screen.getByTestId(
      "settings-integrations-manage-link",
    ) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/admin/integrations");
  });

  it("hides the Manage link for non-admin users", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    );

    const Page = await seedAuth({ admin: false });
    renderInRouter(Page);

    await waitFor(() => {
      expect(screen.getByTestId("settings-integrations-empty")).toBeTruthy();
    });
    expect(
      screen.queryByTestId("settings-integrations-manage-link"),
    ).toBeNull();
    expect(screen.getByText(/admin manages the list/i)).toBeTruthy();
  });

  it("shows the empty-state message when the catalogue is empty", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    );

    const Page = await seedAuth({ admin: false });
    renderInRouter(Page);

    await waitFor(() => {
      expect(screen.getByTestId("settings-integrations-empty")).toBeTruthy();
    });
  });

  it("surfaces an error message when the request fails", async () => {
    // useIntegrationsList retries once on error, so simulate two consecutive
    // failures to drive the query into its error terminal state.
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "boom" }), { status: 500 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "boom" }), { status: 500 }),
      )
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: "boom" }), { status: 500 }),
      );

    const Page = await seedAuth({ admin: false });
    renderInRouter(Page);

    await waitFor(
      () => {
        expect(screen.getByTestId("settings-integrations-error")).toBeTruthy();
      },
      { timeout: 4_000 },
    );
  });
});
