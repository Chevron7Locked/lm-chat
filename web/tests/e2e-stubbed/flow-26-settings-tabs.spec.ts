/**
 * Flow 26 — Settings tabs URL sync (P13a, fixes S-08 / S-12 + P13l.4).
 *
 * What it proves (route-stubbed):
 *  1. Visiting ``/settings`` exposes all nav tabs (per pages/Settings.tsx
 *     NAV_GROUPS — 4 of the 12 are admin-only: LM Studio, Providers,
 *     Preset models, MCP Servers; the test signs in as an admin so all
 *     12 render).
 *  2. Clicking each tab sets the URL to ``/settings/:tab`` and marks the
 *     clicked tab as ``aria-selected="true"`` (ARIA tabs pattern — not
 *     aria-current).
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const TABS = [
  { id: "profile", label: "Profile" },
  { id: "login-security", label: "Login & Security" },
  { id: "lm-studio", label: "LM Studio" },
  { id: "providers", label: "Providers" },
  { id: "preset-models", label: "Preset models" },
  { id: "memory-settings", label: "Memory" },
  { id: "mcp-servers", label: "MCP Servers" },
  { id: "integrations", label: "Integrations" },
  { id: "appearance", label: "Appearance" },
  { id: "chat", label: "Chat" },
  { id: "quota", label: "Quota" },
  { id: "developer", label: "Developer" },
];

test.describe("Flow 26 — Settings tabs URL sync", () => {
  test.beforeEach(async ({ page }) => {
    // Authed as an admin so the admin-only tabs (LM Studio, Providers,
    // Preset models, MCP Servers) render alongside the rest.
    await bootstrapAuthedApp(page, { isAdmin: true });
    await page.goto("/");
  });

  test("all tabs are visible on /settings", async ({ page }) => {
    await page.goto("/settings");
    for (const t of TABS) {
      await expect(page.getByTestId(`settings-tab-${t.id}`)).toBeVisible();
    }
  });

  test("each tab click updates the URL and selection", async ({ page }) => {
    await page.goto("/settings");
    for (const t of TABS) {
      await page.getByTestId(`settings-tab-${t.id}`).click();
      await page.waitForURL((url) => url.pathname === `/settings/${t.id}`);
      await expect(page.getByTestId(`settings-tab-${t.id}`)).toHaveAttribute(
        "aria-selected", "true"
      );
    }
  });
});
