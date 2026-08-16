/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J17 — clicking Resend or Regenerate must never make the boundary USER
 * message vanish from the DOM for the duration of the replay generation.
 *
 * THE BUG: both actions delete-then-replay through the same backend
 * mechanism — delete_from_user_message_for_resend / delete_assistant_turn_
 * for_regenerate both delete the boundary USER message row itself, not
 * just what follows it (see Chat.tsx's submitTurn, which documents this
 * directly). Before the fix, callers of submitTurn (regenerate/resend/
 * edit) went straight to startStream with no optimistic row: the server
 * had already deleted the boundary user message, the messages refetch
 * dropped it from serverMessages, and nothing replaced it until the
 * replayed turn's NEW row was refetched at stream-complete — the resent/
 * regenerated message vanished from the DOM for the FULL duration of
 * generation, which on a real local model is tens of seconds to minutes,
 * not a flicker. The fix is an optimistic `pendingUser` bubble
 * (Chat.tsx submitTurn, consumed by deriveMessageList's
 * optimisticUserMessages).
 *
 * A pass/fail journey that only checks the FINAL state after generation
 * completes cannot see this — the message is back by the time any such
 * check runs. This journey instead SAMPLES across the whole generation,
 * mirroring J11/J13's dwell-and-sample pattern, asserting a user-role row
 * is visible at EVERY sample, never zero, for the entire duration.
 */
import type { Page } from "@playwright/test";
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import { classifyFleet, configureLmStudio } from "./_dogfood-helpers";

// Model-gated: first-token latency + a full replay generation on a
// cold/loaded local model.
const STREAM_START_TIMEOUT_MS = 1_800_000;
const TURN_TIMEOUT_MS = 1_800_000;

// NOT model-gated — real wall-clock sampling cadence across the whole
// generation, not a single instant check.
const DWELL_SAMPLE_INTERVAL_MS = 2_000;

// Long enough that the replay generation runs for real wall-clock time on
// a fast local model too, giving the sampler multiple real chances to
// observe a vanish window if the optimistic bubble regresses.
const LONG_PROMPT =
  "List and describe, in detail, all 20 standard amino acids used in human " +
  "proteins — one full paragraph per amino acid, covering its side-chain " +
  "structure and its biological role. Do not use any tools; just answer " +
  "from what you know, and don't stop until you've covered all 20.";

async function waitForModelSelector(page: Page): Promise<void> {
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
}

/** Fill the composer, submit, and wait until the stream has genuinely started. */
async function sendPromptUntilStreaming(page: Page, prompt: string): Promise<void> {
  const composer = page.getByPlaceholder(/Message/);
  await composer.waitFor({ state: "visible", timeout: 15_000 });
  await composer.fill(prompt);
  await page.keyboard.press("Enter");
  await expect(
    page.getByTestId("chat-message-stream-caret"),
  ).toBeVisible({ timeout: STREAM_START_TIMEOUT_MS });
}

interface ChatDetailJ17 {
  messages: Array<{ id: number; role: string; content: string }>;
}

async function getChat(page: Page, backendURL: string, chatId: number): Promise<ChatDetailJ17> {
  const resp = await page.request.get(`${backendURL}/api/chats/${String(chatId)}`);
  expect(resp.ok(), `GET /api/chats/${String(chatId)} → HTTP ${String(resp.status())}`).toBe(
    true,
  );
  return (await resp.json()) as ChatDetailJ17;
}

/**
 * Sample the DOM every `DWELL_SAMPLE_INTERVAL_MS` while streaming,
 * asserting a user-role message row is visible at EVERY sample. Stops as
 * soon as the composer's send button returns to "Send" (generation
 * finished) or `ceilingMs` is hit.
 */
async function assertUserRowNeverVanishes(
  page: Page,
  label: string,
  ceilingMs: number,
): Promise<void> {
  const deadline = Date.now() + ceilingMs;
  let samples = 0;
  while (Date.now() < deadline) {
    const sendText = await page
      .getByTestId("composer-send-btn")
      .innerText()
      .catch(() => "Send");
    const userRowCount = await page.locator('[data-message-role="user"]').count();
    console.log(
      `J17 DIAG [${label}] sample ${String(samples)}: sendBtn="${sendText}" ` +
        `userRows=${String(userRowCount)}`,
    );
    expect(
      userRowCount,
      `[${label}] the boundary user message vanished from the DOM mid-generation ` +
        `(sample ${String(samples)}) — the optimistic pendingUser bubble did not cover ` +
        "this gap",
    ).toBeGreaterThan(0);
    samples++;
    if (/^Send$/.test(sendText.trim())) break;
    await page.waitForTimeout(DWELL_SAMPLE_INTERVAL_MS);
  }
  expect(
    samples,
    `[${label}] never observed a single sample — the generation may have finished before ` +
      "the first check, weakening this journey's coverage of the vanish window",
  ).toBeGreaterThan(0);
}

test(
  "j17a: Resend keeps the user message visible in the DOM for the whole replay generation",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatId = await createChatViaRequest(page, backendURL, "J17a Resend");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await waitForModelSelector(page);

    // Turn 1 — establish a real user+assistant pair to resend. Uses
    // LONG_PROMPT (not a trivial one-liner) so the REPLAY below also runs
    // long enough on a fast local model for the sampler to matter.
    await sendPromptUntilStreaming(page, LONG_PROMPT);
    await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send$/, {
      timeout: TURN_TIMEOUT_MS,
    });
    const userMsg = page.locator('[data-message-role="user"]').first();
    const userMsgId = await userMsg.getAttribute("data-message-id");
    expect(
      userMsgId,
      "user message row has no data-message-id to target Resend on",
    ).not.toBeNull();

    // Click Resend — this deletes the boundary row server-side and replays
    // it as a fresh (long, slow) turn. THE ACT under test.
    await page.getByTestId(`chat-message-resend-btn-${String(userMsgId ?? "")}`).click();
    await expect(
      page.getByTestId("chat-message-stream-caret"),
    ).toBeVisible({ timeout: STREAM_START_TIMEOUT_MS });

    await assertUserRowNeverVanishes(page, "j17a-resend", TURN_TIMEOUT_MS);

    await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send$/, {
      timeout: TURN_TIMEOUT_MS,
    });
    const finalChat = await getChat(page, backendURL, chatId);
    expect(
      finalChat.messages.map((m) => m.role),
      `expected [user, assistant] after resend, got: ` +
        `${JSON.stringify(finalChat.messages.map((m) => m.role))}`,
    ).toEqual(["user", "assistant"]);
    expect(
      (finalChat.messages[1]?.content ?? "").length,
      "resent turn's assistant content is empty",
    ).toBeGreaterThan(0);

    assertNoConsoleErrors(collectErrors(), "j17a");
  },
);

test(
  "j17b: Regenerate keeps the user message visible in the DOM for the whole replay generation",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatId = await createChatViaRequest(page, backendURL, "J17b Regenerate");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await waitForModelSelector(page);

    await sendPromptUntilStreaming(page, LONG_PROMPT);
    await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send$/, {
      timeout: TURN_TIMEOUT_MS,
    });
    const assistantMsg = page.locator('[data-message-role="assistant"]').first();
    const assistantMsgId = await assistantMsg.getAttribute("data-message-id");
    expect(
      assistantMsgId,
      "assistant message row has no data-message-id to target Regenerate on",
    ).not.toBeNull();

    // Regenerate deletes the boundary USER message too (same delete-then-
    // replay mechanism as Resend — see this file's docstring), so the same
    // vanish window applies to it, not just to Resend.
    await page
      .getByTestId(`chat-message-regenerate-btn-${String(assistantMsgId ?? "")}`)
      .click();
    await expect(
      page.getByTestId("chat-message-stream-caret"),
    ).toBeVisible({ timeout: STREAM_START_TIMEOUT_MS });

    await assertUserRowNeverVanishes(page, "j17b-regenerate", TURN_TIMEOUT_MS);

    await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send$/, {
      timeout: TURN_TIMEOUT_MS,
    });
    const finalChat = await getChat(page, backendURL, chatId);
    expect(
      finalChat.messages.map((m) => m.role),
      `expected [user, assistant] after regenerate, got: ` +
        `${JSON.stringify(finalChat.messages.map((m) => m.role))}`,
    ).toEqual(["user", "assistant"]);
    expect(
      (finalChat.messages[1]?.content ?? "").length,
      "regenerated turn's assistant content is empty",
    ).toBeGreaterThan(0);

    assertNoConsoleErrors(collectErrors(), "j17b");
  },
);
