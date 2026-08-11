/**
 * Flow 9 — Slash `/help` command.
 *
 * What it proves:
 *   When a user types /help in the Composer and submits:
 *     1. An info toast appears listing all available slash commands.
 *     2. The toast contains the names of all BUILTIN_COMMANDS defined in
 *        SlashMenu.tsx (at minimum: /memory, /fork, /compact, /clear, /help,
 *        /panel, /prompt).
 *     3. The toast uses aria-live="polite" (info variant).
 *
 * Source: Composer.tsx dispatchSlashCommand("help"):
 *   push({
 *     variant: "info",
 *     message: BUILTIN_COMMANDS.map((c) => `/${c.name}: ${c.description}`).join("\n"),
 *     duration: 8_000,
 *   })
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  openNewChatInSpa,
} from "./_flow-helpers";

/** Known slash commands that MUST appear in the /help toast. */
const REQUIRED_COMMANDS = [
  "/memory",
  "/fork",
  "/compact",
  "/clear",
  "/help",
  "/panel",
];

test(
  "flow-09: /help shows info toast listing all slash commands",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);
    await openNewChatInSpa(page);

    const composer = page.getByPlaceholder(/Message/);
    await expect(composer).toBeVisible({ timeout: 8_000 });

    // Submit /help.
    await composer.fill("/help");
    await composer.press("Meta+Enter");

    // Info toast appears with role="alert".
    const helpToast = page.getByRole("alert");
    await expect(helpToast).toBeVisible({ timeout: 8_000 });

    // The toast should use aria-live="polite" (info variant).
    await expect(helpToast).toHaveAttribute("aria-live", "polite");

    // Read the toast text and confirm all required commands are listed.
    const toastText = await helpToast.textContent() ?? "";

    for (const cmd of REQUIRED_COMMANDS) {
      expect(
        toastText.toLowerCase(),
        `Toast should contain "${cmd}"`
      ).toContain(cmd.toLowerCase());
    }

    assertNoConsoleErrors(collectErrors(), "flow-09");
  }
);
