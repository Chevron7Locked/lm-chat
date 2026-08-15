/**
 * axe-core accessibility sweep — P9a Item C + §2B Accessibility extensions.
 *
 * WCAG 2.1 AA conformance check across the major SPA routes PLUS the new
 * surfaces: composer, sub-session panel, slash palette, sidebar DnD handles,
 * and model picker.
 *
 * Dual-theme testing:
 *   dark-mode  — 0-violation target for WCAG 2a+2aa
 *   light-mode — known-fail allowlist (shrink-only) loaded from
 *                known-fail-light-mode.json; the suite FAILS if the
 *                allowlist GROWS (i.e. new violations appear).
 *
 * Runs against the live FastAPI backend + stub LM Studio upstream
 * provided by the shared _fixtures.ts worker fixture.
 */

import { test, expect } from "../e2e-live/_fixtures";
import AxeBuilder from "@axe-core/playwright";
import { readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

// ESM-compatible __dirname.
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// ---------------------------------------------------------------------------
// Known-fail allowlist for light mode (shrink-only)
// ---------------------------------------------------------------------------

interface AllowlistEntry {
  rule: string;
  target: string;
  reason: string;
}

function loadLightModeAllowlist(): AllowlistEntry[] {
  try {
    const jsonPath = join(__dirname, "known-fail-light-mode.json");
    const raw = readFileSync(jsonPath, "utf-8");
    const parsed = JSON.parse(raw) as { allowlist?: AllowlistEntry[] };
    return parsed.allowlist ?? [];
  } catch {
    return [];
  }
}

// FROZEN baseline: expected allowlist size.
// This is a hardcoded constant, NOT derived from the live allowlist file,
// so any growth in the file causes a test failure (non-tautological ratchet).
// Bump this only when violations are knowingly accepted.
const EXPECTED_ALLOWLIST_SIZE = 0;

// ---------------------------------------------------------------------------
// Login helper
// ---------------------------------------------------------------------------

async function loginAndWait(
  page: import("@playwright/test").Page,
  backendURL: string,
  username: string,
  password: string
): Promise<void> {
  await page.goto(backendURL);
  await page.getByLabel("Username").fill(username);
  await page.locator("#lmchat-password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${backendURL}/`, { timeout: 10_000 });
}

// ---------------------------------------------------------------------------
// Theme helper — seed theme via localStorage before page load
// ---------------------------------------------------------------------------

async function seedTheme(
  page: import("@playwright/test").Page,
  theme: "dark" | "light"
): Promise<void> {
  await page.addInitScript((t: string) => {
    localStorage.setItem("lmchat:theme", t);
  }, theme);
}

// ---------------------------------------------------------------------------
// Known exceptions (moderate/minor only — critical/serious must be fixed)
// ---------------------------------------------------------------------------
const KNOWN_MINOR_EXCEPTIONS: { rule: string; target: string; reason: string }[] = [];

// ---------------------------------------------------------------------------
// Helper: run axe with WCAG 2a + 2aa tags, checking against allowlist
// ---------------------------------------------------------------------------

async function checkA11y(
  page: import("@playwright/test").Page,
  routeLabel: string,
  theme: "dark" | "light" = "dark"
): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();

  const blockers = results.violations.filter(
    (v) => v.impact === "critical" || v.impact === "serious"
  );

  if (theme === "light") {
    // Light mode: check against the shrink-only allowlist.
    const allowlist = loadLightModeAllowlist();
    // Assert the allowlist has NOT grown since the frozen baseline.
      expect(
        allowlist.length,
        `Light-mode allowlist grew from ${String(EXPECTED_ALLOWLIST_SIZE)} to ${String(allowlist.length)} — new violations must be reviewed`
      ).toBeLessThanOrEqual(EXPECTED_ALLOWLIST_SIZE);

      // Match on BOTH rule ID AND target (selector), so a prior exception for
      // one element does not blanket-suppress new occurrences of the same rule.
      const unlisted = blockers.filter(
        (v) =>
          !allowlist.some((e) =>
            e.rule === v.id &&
            v.nodes.some((n) => n.target.includes(e.target))
          )
      );

    if (unlisted.length > 0) {
      const msg = unlisted
        .map(
          (v) =>
            `[${String(v.impact)}] ${v.id}: ${v.description} (${String(v.nodes.length)} node(s)) — ${routeLabel} [light-mode, not in allowlist]`
        )
        .join("\n");
      throw new Error(
        `axe critical/serious violations on ${routeLabel} (light-mode):\n${msg}`
      );
    }

    // Log allowlisted violations for awareness.
    for (const v of blockers) {
      if (allowlist.some((e) => e.rule === v.id)) {
        console.warn(
          `[light-mode allowlisted] ${v.id} on ${routeLabel}: ${v.description}`
        );
      }
    }
  } else {
    // Dark mode (default): strict 0-violation check.
    if (blockers.length > 0) {
      const msg = blockers
        .map(
          (v) =>
            `[${String(v.impact)}] ${v.id}: ${v.description} (${String(v.nodes.length)} node(s)) — ${routeLabel}`
        )
        .join("\n");
      throw new Error(`axe critical/serious violations on ${routeLabel}:\n${msg}`);
    }
  }

  // Log moderate/minor for the record — do not fail.
  const other = results.violations.filter(
    (v) => v.impact !== "critical" && v.impact !== "serious"
  );
  for (const v of other) {
    const isKnown = KNOWN_MINOR_EXCEPTIONS.some((e) => e.rule === v.id);
    if (!isKnown && v.nodes.length > 0) {
      console.warn(
        `[axe ${v.impact ?? "unknown"}] ${v.id} on ${routeLabel}: ${v.description}`
      );
    }
  }

  // Always pass the Playwright expectation — only critical/serious fail (or light-mode allowlist gate).
  if (theme === "dark") {
    expect(blockers).toHaveLength(0);
  }
}

// ---------------------------------------------------------------------------
// Helper: create a chat for testing (returns chat ID)
// ---------------------------------------------------------------------------

async function createChatForTest(
  page: import("@playwright/test").Page,
  backendURL: string
): Promise<number> {
  const chatId = await page.evaluate(async (url: string) => {
    const resp = await fetch(`${url}/api/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ title: "A11y Test Chat" }).toString(),
      credentials: "include",
    });
    if (!resp.ok) throw new Error(`POST /api/chats → ${String(resp.status)}`);
    const data = await resp.json() as { id: number };
    return data.id;
  }, backendURL);
  return chatId;
}

// ---------------------------------------------------------------------------
// Helper: navigate to the chat page (creating one if needed)
// ---------------------------------------------------------------------------

async function navigateToChatPage(
  page: import("@playwright/test").Page,
  backendURL: string
): Promise<number> {
  const chatId = await page.evaluate(async (url: string) => {
    const resp = await fetch(`${url}/api/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ title: "A11y Test Chat" }).toString(),
      credentials: "include",
    });
    if (!resp.ok) throw new Error(`POST /api/chats → ${String(resp.status)}`);
    const data = await resp.json() as { id: number };
    return data.id;
  }, backendURL);

  await page.goto(`${backendURL}/chats/${String(chatId)}`);
  await page.waitForURL(`${backendURL}/chats/${String(chatId)}`, { timeout: 10_000 });
  await page.waitForLoadState("networkidle", { timeout: 10_000 });
  return chatId;
}

// ---------------------------------------------------------------------------
// Screens under test
// ---------------------------------------------------------------------------

const SCREENS: { label: string; setup: (page: import("@playwright/test").Page, backendURL: string) => Promise<void> }[] = [
  {
    label: "composer",
    setup: async (page, backendURL) => {
      await navigateToChatPage(page, backendURL);
      // Wait for the composer textarea to be present.
      await page.locator(".lmchat-composer-textarea").waitFor({ state: "visible", timeout: 10_000 });
    },
  },
  {
    label: "sidebar_dnd",
    setup: async (page, backendURL) => {
      await navigateToChatPage(page, backendURL);
      // Wait for the sidebar with sortable chat items and drag handles.
      await page.locator(".lmchat-sidebar").waitFor({ state: "visible", timeout: 10_000 });
      // Ensure at least one drag handle is rendered.
      await page.locator(".lmchat-drag-handle").first().waitFor({ state: "visible", timeout: 10_000 }).catch(() => {
        // No drag handles visible — chat list may be empty; that's acceptable.
      });
    },
  },
  {
    label: "slash_palette",
    setup: async (page, backendURL) => {
      await navigateToChatPage(page, backendURL);
      // Open the slash palette via Cmd+/ (or Ctrl+/ on Windows/Linux).
      await page.locator(".lmchat-composer-textarea").waitFor({ state: "visible", timeout: 10_000 });
      await page.locator(".lmchat-composer-textarea").press("Meta+/");
      await page.waitForTimeout(500);
      // If the palette didn't open via Cmd+/, try focusing the composer and typing "/".
      const paletteVisible = await page.locator(".lmchat-palette-card").isVisible().catch(() => false);
      if (!paletteVisible) {
        await page.locator(".lmchat-composer-textarea").fill("/");
        await page.waitForTimeout(300);
      }
    },
  },
{
      label: "model_picker",
      setup: async (page, backendURL) => {
        await navigateToChatPage(page, backendURL);
        // The model picker (ModelSelectControl) is in the chat page header.
        // Look for a <select> element or the ModelSelectControl component.
        // Fail if the model picker is not visible — never silently fall back
        // to a generic page-level scan.
        await page.locator("select, [data-testid*='model'], .lmchat-model-select").first().waitFor({
          state: "visible",
          timeout: 10_000,
        });
      },
    },
    {
      label: "sub_session_panel",
      setup: async (page, backendURL) => {
        await navigateToChatPage(page, backendURL);
        // Sub-session panel is rendered when a sub-session is active.
        // Check for the outer wrapper. If the panel isn't available in this
        // environment the test will fail (no silent fallback).
        await page.locator(".lmchat-subsession-outer").waitFor({
          state: "visible",
          timeout: 5_000,
        });
      },
    },
];

// ---------------------------------------------------------------------------
// Tests — dark mode (default, 0-violation target)
// ---------------------------------------------------------------------------

test.describe("axe-core WCAG 2a+2aa sweep — dark mode (0-violation)", () => {
  // Re-run the legacy page-level tests under the new tag set (wcag2a+wcag2aa).
  test("login page", async ({ page, backendURL }) => {
    await page.goto(backendURL);
    await page.getByLabel("Username").waitFor({ state: "visible", timeout: 10_000 });
    await checkA11y(page, "/login", "dark");
  });

  test("chat home page", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.waitForLoadState("networkidle", { timeout: 10_000 });
    await checkA11y(page, "/", "dark");
  });

  test("chat page (with created chat)", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    const chatId = await createChatForTest(page, backendURL);
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.waitForURL(`${backendURL}/chats/${String(chatId)}`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 10_000 });
    await checkA11y(page, `/chats/${String(chatId)}`, "dark");
  });

  test("settings page", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.goto(`${backendURL}/settings`);
    await page.waitForURL(`${backendURL}/settings`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 });
    await checkA11y(page, "/settings", "dark");
  });

  test("memory page", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.goto(`${backendURL}/memory`);
    await page.waitForURL(`${backendURL}/memory`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 });
    await checkA11y(page, "/memory", "dark");
  });

  test("documents page", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.goto(`${backendURL}/documents`);
    await page.waitForURL(`${backendURL}/documents`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 });
    await checkA11y(page, "/documents", "dark");
  });

  test("analytics page", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.goto(`${backendURL}/analytics`);
    await page.waitForURL(`${backendURL}/analytics`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 });
    await checkA11y(page, "/analytics", "dark");
  });

  test("prompts page", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.goto(`${backendURL}/prompts`);
    await page.waitForURL(`${backendURL}/prompts`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 });
    await checkA11y(page, "/prompts", "dark");
  });

  test("plugins page", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);
    await page.goto(`${backendURL}/plugins`);
    await page.waitForURL(`${backendURL}/plugins`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 });
    await checkA11y(page, "/plugins", "dark");
  });

  test("compare page — no critical/serious violations", async ({ page, backendURL, testUsername, testPassword }) => {
    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Create a chat with ab_compare enabled.
    const chatId = await page.evaluate(async (url: string) => {
      const resp = await fetch(`${url}/api/chats`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ title: "A11y AB Compare Chat" }).toString(),
        credentials: "include",
      });
      if (!resp.ok) throw new Error(`POST /api/chats → ${String(resp.status)}`);
      const data = await resp.json() as { id: number };
      return data.id;
    }, backendURL);

    // Enable A/B compare mode on the chat.
    await page.evaluate(
      async ({ url, id }: { url: string; id: number }) => {
        const body = new URLSearchParams({
          ab_compare: JSON.stringify({
            enabled: true,
            model_a: "stub-a",
            model_b: "stub-b",
          }),
        });
        await fetch(`${url}/api/chats/${String(id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body.toString(),
          credentials: "include",
        });
      },
      { url: backendURL, id: chatId }
    );

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.waitForURL(`${backendURL}/chats/${String(chatId)}`, { timeout: 10_000 });
    await page.waitForLoadState("networkidle", { timeout: 10_000 });
    await checkA11y(page, "/compare", "dark");
  });

  // ── New screens (dark mode) ────────────────────────────────────────────────

  for (const screen of SCREENS) {
    test(`${screen.label} — no critical/serious violations`, async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);
      await screen.setup(page, backendURL);
      await checkA11y(page, `/${screen.label}`, "dark");
    });
  }
});

// ---------------------------------------------------------------------------
// Tests — light mode (known-fail allowlist, shrink-only)
// ---------------------------------------------------------------------------

test.describe("axe-core WCAG 2a+2aa sweep — light mode (known-fail allowlist)", () => {
  for (const screen of SCREENS) {
    test(`${screen.label} — no unlisted critical/serious violations`, async ({ page, backendURL, testUsername, testPassword }) => {
      // Seed light mode theme before any page navigation.
      await seedTheme(page, "light");
      await loginAndWait(page, backendURL, testUsername, testPassword);
      await screen.setup(page, backendURL);
      await checkA11y(page, `/${screen.label}`, "light");
    });
  }
});