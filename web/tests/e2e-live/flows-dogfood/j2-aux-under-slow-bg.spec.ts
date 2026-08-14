/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J2 (crown journey) — background aux calls under a SLOW reasoning model.
 *
 * This is the journey the mocked suite structurally CANNOT run and the exact
 * one that was silently broken in the 2026-07-18 dogfood: auto-memory
 * distillation produced zero facts because a real reasoning background-model
 * (~42s) blew the then-30s aux timeout, and the failure was invisible in the UI.
 *
 * We deliberately pin the background-tasks model to the SLOWEST loaded LLM (the
 * pathological-but-real condition) and assert on the BACKEND LOG that the
 * fail-soft aux ops actually SUCCEEDED — not on the UI, which shows a benign
 * fallback when they fail. Reverting the aux-timeout fix (f4de4d9 / 334ae5f)
 * turns this red; the mocked flow-24 / flow-29 stay green.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import {
  classifyFleet,
  configureLmStudio,
  pinBackgroundModel,
  sendTurnAndWait,
  assertAutoMemoryDistilled,
  assertAutoTitleFromLlm,
  assertNoLogLine,
} from "./_dogfood-helpers";

// Slow reasoning models are slow: a single aux call is ~40-90s, and distill /
// title / follow-ups contend on the one background model. Budget generously —
// this is a pre-ship gate, not the inner loop.
//
// The distill wait MATCHES the app's own aux budget (lm_chat_aux_model_timeout_sec,
// default 900s): auto-memory is a fire-and-forget local-first task, so the app
// will keep working up to 900s on a cold/loaded model. A shorter test wait would
// give up while the app is still (correctly) distilling — the same cloud-timeframe
// assumption we removed from the app. These are POLL CEILINGS: the assertion
// returns the instant a fact lands, so on a warm box J2 still finishes in seconds.
const TURN_TIMEOUT_MS = 1_800_000;
const DISTILL_TIMEOUT_MS = 900_000;

test(
  "j2: auto-memory + title + follow-ups succeed on a SLOW background model (grey-box)",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    // Ceiling covers TURN + DISTILL waits (1800s + 900s = 2700s) plus overhead
    // on a pathologically-loaded box; a no-op on a warm box where J2 finishes fast.
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    // Log in as ADMIN first — pinning the background-tasks model is an admin
    // setting, and classify/pin below authenticate via the page's cookies.
    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    // Classify the loaded fleet. The chat runs on the FAST model (realistic),
    // but the background aux tasks are pinned to the SLOWEST model — the
    // pathological condition that silently broke auto-memory in the dogfood.
    // With the aux-serialization gate (bg_aux) each aux call now runs alone on
    // the slow model within its own timeout budget; REMOVING that gate makes the
    // three calls contend and all time out → this journey goes red. That is the
    // red-on-revert coverage for both the serialization AND timeout fixes.
    const fleet = await classifyFleet(page, backendURL);
    if (fleet.slowIsSameAsFast) {
      test.info().annotations.push({
        type: "warn",
        description:
          `j2: only one model-size class loaded (${fleet.slowId}); the slow-bg ` +
          `dimension is reduced. Load a larger reasoning model for full coverage.`,
      });
    }
    await configureLmStudio(page, backendURL, fleet.fastId);
    await pinBackgroundModel(page, backendURL, fleet.slowId);

    const chatId = await createChatViaRequest(page, backendURL, "New Chat");

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    // Wait for the model selector to populate (mirror flow-24's guard).
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]',
        );
        if (sel === null) return false;
        return sel.value !== "" && sel.options.length > 1;
      },
      null,
      { timeout: 30_000 },
    );

    // A fact-laden turn so distillation has durable facts to extract.
    await sendTurnAndWait(
      page,
      "For the record: my name is Dana, I live in Portland, and I work as a " +
        "marine biologist. In one short sentence, what ocean is Portland nearest?",
      TURN_TIMEOUT_MS,
    );

    // GREY-BOX: all three fail-soft aux ops are INVISIBLE in the UI when they
    // fail. Assert distill via the REAL stored outcome (the memory API) and
    // title/followups via the reliably-teed WARNING failure markers.
    await assertAutoMemoryDistilled(
      page,
      backendURL,
      backendLogPath,
      DISTILL_TIMEOUT_MS,
    );
    assertAutoTitleFromLlm(backendLogPath, chatId);
    assertNoLogLine(
      backendLogPath,
      ["stream.followups_oob_failed"],
      "follow-up chip generation failed (aux call errored/timed out)",
    );

    // Belt-and-suspenders foreground check: the distilled facts surface on the
    // Memory page's "Remembered automatically" section.
    await page.goto(`${backendURL}/memory`);
    const autoSection = page.getByRole("region", {
      name: /Remembered automatically/i,
    });
    await expect(autoSection).toBeVisible({ timeout: 15_000 });
    await expect(autoSection.getByRole("listitem").first()).toBeVisible({
      timeout: 15_000,
    });

    // Restore the background model to FAST so the slow-model pin does not leak
    // into later journeys in the same worker (else J3/J4/J5 inherit bg=slow and
    // hammer the big model, degrading the whole run).
    await pinBackgroundModel(page, backendURL, fleet.fastId);

    assertNoConsoleErrors(collectErrors(), "j2");
  },
);
