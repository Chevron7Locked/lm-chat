/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J15 — incognito chat privacy invariants, against a REAL model.
 *
 * No prior dogfood journey creates an incognito chat at all. Three
 * server-enforced invariants (chats.py / memory distillation gate):
 *   1. an incognito chat's turns never feed auto-memory distillation —
 *      streaming_service.py explicitly skips it (`stream.distill.
 *      skipped_incognito`) even for a fact-laden turn that J2 proves DOES
 *      distill on a normal chat — this is the live-model half of the
 *      invariant, the one a unit test feeding a canned reply can't catch
 *      (nothing here exercises whether the REAL model's fact-laden answer
 *      would otherwise have been distilled);
 *   2. an incognito chat cannot be shared (`POST .../share` → 403);
 *   3. an incognito chat cannot be promoted to a project
 *      (`POST .../promote-to-project` → 422).
 * (2) and (3) are pure backend invariants (no model dependency) included
 * here because no other dogfood journey ever creates an incognito chat to
 * exercise them against a real, running backend.
 *
 * GREY-BOX for (1): GET /api/memory/auto is the same real-outcome API J2
 * polls to prove distillation SUCCEEDED; here it is the same call proving
 * distillation NEVER STARTED for an otherwise fact-laden turn. cleanState
 * (see _fixtures.ts) truncates `pinned_insights` (auto memories live there)
 * before every test, so this starts from a real, verified-empty baseline,
 * not an assumption.
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

const TURN_TIMEOUT_MS = 1_800_000;
// Distillation is skipped BEFORE any aux model call is attempted (a pure
// in-process gate check), so it never needs anywhere near J2's 900s
// distill-success ceiling. This is a real wall-clock dwell proving the
// negative stays negative, not a single instant check.
const DWELL_SAMPLE_COUNT = 4;
const DWELL_SAMPLE_INTERVAL_MS
  = 5_000;

interface AutoMemory {
  id: number;
}

async function createIncognitoChat(
  page: import("@playwright/test").Page,
  backendURL: string,
  title: string,
): Promise<number> {
  const resp = await page.request.post(`${backendURL}/api/chats`, {
    form: { title, incognito: "true" },
  });
  expect(
    resp.ok(),
    `POST /api/chats (incognito) → HTTP ${String(resp.status())}`,
  ).toBe(true);
  const data = (await resp.json()) as { id: number; incognito: boolean };
  expect(
    data.incognito,
    "chat created with incognito=true did not come back incognito",
  ).toBe(true);
  return data.id;
}

async function fetchAutoMemoryCount(
  page: import("@playwright/test").Page,
  backendURL: string,
): Promise<number> {
  const resp = await page.request.get(`${backendURL}/api/memory/auto`);
  expect(resp.ok(), `GET /api/memory/auto → HTTP ${String(resp.status())}`).toBe(true);
  const body = (await resp.json()) as AutoMemory[];
  return Array.isArray(body) ? body.length : 0;
}

test(
  "j15: an incognito chat's fact-laden turn never distills auto-memory, and cannot be shared or promoted",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    // Verified-empty baseline (cleanState truncates pinned_insights).
    const baseline = await fetchAutoMemoryCount(page, backendURL);
    expect(
      baseline,
      "auto-memory table was not empty at test start — cleanState truncation " +
        "assumption broke, this test's negative assertion would be meaningless",
    ).toBe(0);

    const chatId = await createIncognitoChat(page, backendURL, "J15 Incognito");

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await expect(page.getByTestId("chat-incognito-badge")).toBeVisible({
      timeout: 15_000,
    });
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]',
        );
        return sel !== null && sel.value !== "" && sel.options.length > 1;
      },
      null,
      { timeout: 30_000 },
    );

    // Same fact-laden shape J2 uses to prove distillation DOES succeed on a
    // normal chat — the only difference here is the incognito flag.
    await sendTurnAndWait(
      page,
      "For the record: my name is Jordan, I live in Austin, and I work as " +
        "a structural engineer. In one short sentence, name one famous " +
        "landmark in Austin.",
      TURN_TIMEOUT_MS,
    );

    // The turn itself must still have worked normally (incognito ≠ broken).
    await expect(
      page.locator('[data-message-role="assistant"]').last(),
    ).toBeVisible();

    // Real wall-clock dwell — the negative must hold, not just at t=0.
    for (let i = 0; i < DWELL_SAMPLE_COUNT; i++) {
      const count = await fetchAutoMemoryCount(page, backendURL);
      console.log(`J15 DIAG [sample ${String(i)}]: auto-memory count=${String(count)}`);
      expect(
        count,
        "an incognito chat's fact-laden turn distilled auto-memory anyway " +
          "— the incognito privacy invariant is broken",
      ).toBe(0);
      if (i < DWELL_SAMPLE_COUNT - 1) {
        await page.waitForTimeout(DWELL_SAMPLE_INTERVAL_MS);
      }
    }

    // --- (2) cannot be shared ---
    const shareResp = await page.request.post(
      `${backendURL}/api/chats/${String(chatId)}/share`,
    );
    expect(
      shareResp.status(),
      "sharing an incognito chat did not 403",
    ).toBe(403);

    // --- (3) cannot be promoted to a project ---
    const promoteResp = await page.request.post(
      `${backendURL}/api/chats/${String(chatId)}/promote-to-project`,
      { form: { name: "J15 Promoted" } },
    );
    expect(
      promoteResp.status(),
      "promoting an incognito chat to a project did not 422",
    ).toBe(422);

    assertNoConsoleErrors(collectErrors(), "j15");
  },
);
