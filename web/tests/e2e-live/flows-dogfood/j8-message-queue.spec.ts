/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J8 — the composer message queue (P1-dogfood).
 *
 * RED-ON-REVERT: this journey fails if faac3f8 ("feat(chat): queue a
 * message while a response streams") is reverted. Before that commit the
 * composer had no queue: a message submitted while a response was
 * streaming would either be silently dropped or block on a disabled
 * textarea. faac3f8 keeps the textarea intentionally NEVER
 * `disabled={streaming}` (Composer.tsx) so typing/submitting mid-stream
 * always works, and routes a mid-stream submit into a `queue` array
 * instead of firing it immediately — the head item auto-sends the instant
 * the in-flight turn finishes naturally (the streaming→idle effect in
 * Composer.tsx).
 *
 * Drives a REAL two-turn exchange against a real model: send turn 1, and
 * WHILE it is still streaming, type + submit a second message. Asserts:
 *   1. typing is never blocked mid-stream (the composer stays enabled and
 *      accepts input);
 *   2. the mid-stream submit QUEUES (visible "composer-queue" chip with the
 *      queued text) instead of being sent immediately or dropped;
 *   3. once turn 1's stream finishes on its own, the queued message
 *      AUTO-SENDS as turn 2 with no manual "Send now" click;
 *   4. both turns land in the persisted transcript (GET /api/chats/{id}
 *      returns exactly 4 rows: user, assistant, user, assistant — with the
 *      second user row's content matching what was queued).
 *
 * Pipeline mechanics only — never asserts specific model text.
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
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 1_800_000;

test(
  "j8: a message typed and submitted mid-stream queues, then auto-sends once the current turn finishes",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatId = await createChatViaRequest(page, backendURL, "Message Queue Test");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
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

    const composer = page.getByPlaceholder(/Message/);
    const sendBtn = page.getByTestId("composer-send-btn");
    // A slightly longer ask for turn 1 widens the mid-stream window (more
    // tokens to generate = more wall-clock time to submit turn 2 while it's
    // still in flight) — content is never asserted on, only used to make
    // the race reliable against a fast local model.
    const firstTurnText =
      "In 3-4 sentences, explain how photosynthesis works.";
    const queuedTurnText = "In one sentence, name the capital of Japan.";

    // Turn 1 — START it but DON'T wait for it to finish: we need it still
    // streaming so the NEXT message queues rather than sends. (sendTurnAndWait
    // now waits for turn completion, so it can't be used to start turn 1 here.)
    await composer.fill(firstTurnText);
    await page.keyboard.press("Enter");

    // (1a) Confirm turn 1 is genuinely mid-stream — the send button flips to
    // "Queue" the instant `streaming` goes true (Composer.tsx) and stays there
    // for the whole stream, so this is observed reliably (we did not wait the
    // turn out first, so there is no race against a finished turn).
    await expect(sendBtn).toHaveText(/Queue/, { timeout: TURN_TIMEOUT_MS });

    // (1b) ASSERT typing is allowed during streaming — the textarea must
    // accept input and stay enabled while turn 1 is in flight.
    await expect(composer).toBeEnabled();
    await composer.fill(queuedTurnText);
    await expect(composer).toHaveValue(queuedTurnText);

    // Submit while still streaming — this must QUEUE, not send or drop.
    await page.keyboard.press("Enter");

    // (2) The queue row renders with the queued text, marked "sends next".
    const queue = page.getByTestId("composer-queue");
    await expect(queue).toBeVisible({ timeout: 10_000 });
    const queueItem = page.getByTestId("composer-queue-item").first();
    await expect(queueItem).toBeVisible();
    await expect(queueItem).toContainText(queuedTurnText);
    await expect(queueItem).toContainText("Queued — sends next");
    // Remove is always available; "Send now" is streaming-gated (hidden
    // while streaming — Composer.tsx's `{!streaming && (...)}`).
    await expect(page.getByTestId("composer-queue-remove")).toBeVisible();
    await expect(page.getByTestId("composer-queue-send-now")).toHaveCount(0);

    // The composer clears its own text once a submit is accepted (sent OR
    // queued) — confirms the queue swallowed the submit rather than
    // leaving it stuck, unsent, in the textarea.
    await expect(composer).toHaveValue("");

    // (3) Once turn 1's stream finishes NATURALLY, the queue auto-drains:
    // the row disappears and the queued message becomes turn 2.
    await expect(queue).toHaveCount(0, { timeout: TURN_TIMEOUT_MS });

    // Turn 2 must now be running / finish on its own — wait for the
    // composer to settle back to idle ("Send") once BOTH turns are done.
    await expect(sendBtn).toHaveText(/Send$/, { timeout: TURN_TIMEOUT_MS });

    // Belt-and-suspenders UI check: two assistant turns rendered.
    await expect(page.locator('[data-message-role="assistant"]')).toHaveCount(
      2,
      { timeout: TURN_TIMEOUT_MS },
    );

    // (4) GROUND TRUTH — verify via the persisted transcript, not just the
    // DOM.
    const detailResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`,
    );
    expect(detailResp.ok()).toBeTruthy();
    const detail = (await detailResp.json()) as {
      messages: Array<{ id: number; role: string; content: string }>;
    };
    expect(
      detail.messages.length,
      "expected 2 turns x (user+assistant) = 4 persisted rows, got: " +
        JSON.stringify(detail.messages.map((m) => m.role)),
    ).toBe(4);
    expect(detail.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(
      detail.messages[2]?.content,
      "the queued message's text did not land as the second user turn",
    ).toBe(queuedTurnText);
    expect(
      (detail.messages[1]?.content ?? "").length,
      "turn 1's assistant content is empty",
    ).toBeGreaterThan(0);
    expect(
      (detail.messages[3]?.content ?? "").length,
      "auto-sent turn 2's assistant content is empty — the queue drained " +
        "but the second turn produced nothing",
    ).toBeGreaterThan(0);

    assertNoConsoleErrors(collectErrors(), "j8");
  },
);
