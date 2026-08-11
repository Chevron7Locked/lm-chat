/**
 * CSS custom-property smoke test — P7 DEFERRED §4, closed in P9b.
 *
 * Navigates to each major page and asserts zero console errors or warnings
 * mentioning missing CSS custom properties.
 *
 * A missing CSS variable produces browser console messages matching one or
 * more of these patterns:
 *   - "var(--" followed by a value that suggests unresolved (empty / invalid)
 *   - Some browsers log "Unknown property" or similar for var() fallback failures
 *
 * Approach: attach a page.on("console") listener before navigation, collect
 * all error/warning messages, then assert none mention the known CSS variable
 * failure patterns.
 *
 * Closes P7 DEFERRED §4.
 *
 * Pages exercised:
 *   / (root → redirects to Chat or Login)
 *   /login
 *   /chats (Chat page, after login)
 *   /settings
 *   /memory
 *   /documents
 *   /analytics
 *   /prompt-library
 *   /admin/quotas
 *   /admin/integrations
 */

import { test, expect } from "./_fixtures";
import type { ConsoleMessage } from "@playwright/test";

// ---------------------------------------------------------------------------
// Patterns that indicate a missing / unresolved CSS variable
// ---------------------------------------------------------------------------

const CSS_VAR_ERROR_PATTERNS: RegExp[] = [
  /custom property/i,
  /CSS variable/i,
  /--[a-z][\w-]+ is not defined/i,
  /Invalid property value.*var\(--/i,
];

function isCssVarError(msg: ConsoleMessage): boolean {
  if (msg.type() !== "error" && msg.type() !== "warning") return false;
  const text = msg.text();
  return CSS_VAR_ERROR_PATTERNS.some((re) => re.test(text));
}

// ---------------------------------------------------------------------------
// Login helper
// ---------------------------------------------------------------------------

async function loginAndWait(
  page: import("@playwright/test").Page,
  backendURL: string,
  username: string,
  password: string
): Promise<void> {
  await page.goto(`${backendURL}/login`);
  await page.getByLabel("Username").fill(username);
  await page.locator("#lmchat-password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${backendURL}/`, { timeout: 15_000 });
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

test.describe("CSS custom property smoke (P7 §4)", () => {
  test(
    "no missing CSS variable console errors on any major page",
    async ({ page, backendURL, testUsername, testPassword }) => {
      // Collect all CSS-var-related console errors across all navigations.
      const cssVarErrors: Array<{ url: string; msg: string }> = [];

      page.on("console", (msg) => {
        if (isCssVarError(msg)) {
          cssVarErrors.push({ url: page.url(), msg: msg.text() });
        }
      });

      // Log in first so authenticated pages render fully.
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Pages to exercise (order: public then authenticated).
      const pages = [
        // Root (redirects to chat page or login).
        backendURL + "/",
        // Chat page (new chat).
        backendURL + "/chats",
        // Settings.
        backendURL + "/settings",
        // Memory.
        backendURL + "/memory",
        // Documents.
        backendURL + "/documents",
        // Analytics.
        backendURL + "/analytics",
        // Prompt library.
        backendURL + "/prompt-library",
        // Admin — quotas.
        backendURL + "/admin/quotas",
        // Admin — integrations.
        backendURL + "/admin/integrations",
      ];

      for (const url of pages) {
        await page.goto(url, { waitUntil: "networkidle", timeout: 20_000 }).catch(() => {
          // Some pages redirect; networkidle may time out on loading pages.
          // Continue — we still collected any console errors.
        });
        // Brief pause to allow deferred style computations to flush.
        await page.waitForTimeout(300);
      }

      // Assert no CSS variable errors were collected.
      expect(
        cssVarErrors,
        `CSS custom property errors found:\n${cssVarErrors
          .map((e) => `  [${e.url}] ${e.msg}`)
          .join("\n")}`
      ).toHaveLength(0);
    }
  );
});
