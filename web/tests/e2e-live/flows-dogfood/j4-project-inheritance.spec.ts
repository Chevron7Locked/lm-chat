/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J4 — project custom-instruction auto-inherit.
 *
 * Creates a project, sets a sentinel custom-instruction system prompt, opens
 * a chat scoped to that project, sends a turn, and asserts the REAL model's
 * reply contains the sentinel tag. Instruction-following is the one
 * reliable proof the project's system prompt was actually injected into the
 * turn — a UI check for "some reply text appeared" would pass even if the
 * project instruction silently failed to thread through.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
} from "../flows/_flow-helpers";
import {
  classifyFleet,
  configureLmStudio,
  sendTurnAndWait,
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 180_000;
const SENTINEL_INSTRUCTION =
  "You are a test bot. End EVERY reply with the exact tag [LMCHAT-J4] on its own line.";
const SENTINEL_TAG = "[LMCHAT-J4]";

test(
  "j4: project custom instructions auto-inherit into a project chat",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    // Open the Projects pane and create a project.
    await page.getByTestId("sidebar-projects-link").click();
    await page.getByTestId("sidebar-new-project-btn").click();
    const projectName = `J4 Project ${String(Date.now())}`;
    await page
      .getByTestId("sidebar-projects-pane-create-input")
      .fill(projectName);
    await page.getByTestId("sidebar-projects-pane-create-submit").click();
    await page.waitForURL(/\/project\/\d+/, { timeout: 15_000 });

    // Settings tab — set the sentinel custom instruction and save.
    await page.getByTestId("project-tab-settings").click();
    await page.getByTestId("project-settings-prompt").fill(SENTINEL_INSTRUCTION);
    await page.getByTestId("project-settings-save").click();
    await expect(page.getByText("Project settings saved.")).toBeVisible({
      timeout: 10_000,
    });

    // Chats tab — create a project-scoped chat; lands on /chats/:id.
    await page.getByTestId("project-tab-chats").click();
    await page.getByTestId("project-new-chat-input").fill("J4 chat");
    await page.getByTestId("project-new-chat-submit").click();
    await page.waitForURL(/\/chats\/\d+/, { timeout: 15_000 });
    // A project chat with no project-level default_model legitimately shows the
    // "Select a model…" placeholder (the backend resolves the global default at
    // send-time), so — unlike a main chat — wait only for the OPTIONS to load,
    // then explicitly select the fast model so the turn has a concrete model.
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]',
        );
        return sel !== null && sel.options.length > 1;
      },
      null,
      { timeout: 30_000 },
    );
    // Select the FASTEST loaded model (smallest active-param rank) by in-page
    // scan. The option VALUEs are wire/instance ids (not the /api/models key),
    // so selectOption(fleet.fastId) hangs; and picking index 1 grabs the SLOWEST
    // model, whose turn can exceed the timeout under load. J4 only needs a model
    // that answers + follows the sentinel rule — the fast one keeps it quick.
    const fastIndex = await page.evaluate(() => {
      const sel = document.querySelector<HTMLSelectElement>(
        '[data-testid="chat-header-model-select"]',
      );
      if (sel === null) return 1;
      const rank = (s: string): number => {
        const t = s.toLowerCase();
        const a = /a(\d+(?:\.\d+)?)b(?![a-z0-9])/.exec(t);
        if (a) return Number.parseFloat(a[1]);
        const n = /(\d+(?:\.\d+)?)b(?![a-z0-9])/.exec(t);
        if (n) return Number.parseFloat(n[1]);
        return Number.POSITIVE_INFINITY;
      };
      let best = 1;
      let bestRank = Number.POSITIVE_INFINITY;
      for (let i = 0; i < sel.options.length; i++) {
        const o = sel.options[i];
        if (o.disabled || o.value === "") continue;
        if (o.text.toLowerCase().includes("unloaded")) continue;
        const r = rank(o.text);
        if (r < bestRank) {
          bestRank = r;
          best = i;
        }
      }
      return best;
    });
    await page
      .getByTestId("chat-header-model-select")
      .selectOption({ index: fastIndex });

    await sendTurnAndWait(page, "Reply with a short greeting.", TURN_TIMEOUT_MS);

    // ASSERT: the project's custom instruction was injected — the reply
    // follows the sentinel formatting rule. sendTurnAndWait already awaited
    // the stream's terminal, so the assistant message is complete by now.
    await expect(
      page.locator('[data-message-role="assistant"]').last(),
    ).toContainText(SENTINEL_TAG, { timeout: 5_000 });

    assertNoConsoleErrors(collectErrors(), "j4");
  },
);
