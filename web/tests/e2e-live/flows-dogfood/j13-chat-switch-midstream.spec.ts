/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J13 — navigating between chats while one of them is mid-stream must never
 * leak state across the chat boundary.
 *
 * THE BUG CLASS THIS GUARDS: `<Chat/>` is mounted ONCE for the whole
 * `/chats/:chatId` route (router.tsx has no `key={chatId}` on it) and
 * `useSSE()`'s `state` is a SINGLE instance that outlives chat navigation
 * (see `StreamState.chatId`'s own doc in useSSE.ts) — so switching chats
 * mid-stream does NOT remount the component tree or reset the in-flight
 * stream's state the way a naive mental model would expect. Two real,
 * shipped bugs lived exactly here:
 *   1. leaving a streaming answer for another chat rendered the FIRST
 *      chat's live text INSIDE the second chat's message list;
 *   2. a completing stream stored its `response_id` (localStorage,
 *      `lmchat:sse:<chatId>:rid` — see lib/responseId.ts) and invalidated
 *      the message-list query cache against WHICHEVER chat was on screen
 *      when the terminal frame arrived, not the chat the stream actually
 *      belonged to — so the next turn in that chat silently continued the
 *      wrong conversation's chain.
 *
 * Both are now guarded in the source by comparing `sseState.chatId`
 * (the chat the shared stream state actually belongs to) against the
 * chat on screen at every consumption site — `deriveMessageList`'s
 * `belongsToThisChat`, the response-id/cache-invalidation effect, the
 * followups/mode_adopt effects, the Composer `streaming` prop, and the
 * per-chat message queue's own `item.chatId` tagging in Composer.tsx. None
 * of that is exercised by any prior dogfood journey — every existing j-file
 * drives exactly ONE chat per test. This is the single highest-value gap:
 * reverting ANY of those `chatId` guards should turn this journey red.
 *
 * Pipeline mechanics + real persisted/localStorage state only — never
 * asserts specific model text.
 */
import type { Page } from "@playwright/test";
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
  navigateToChatInSpa,
} from "../flows/_flow-helpers";
import { classifyFleet, configureLmStudio } from "./_dogfood-helpers";

// Model-gated: first-token latency on a cold/loaded local model.
const STREAM_START_TIMEOUT_MS = 1_800_000;
const TURN_TIMEOUT_MS = 1_800_000;

// NOT model-gated — real wall-clock dwell so a leak that only shows up a
// few seconds in (more deltas arriving) has a real chance to appear.
const DWELL_SAMPLE_COUNT = 4;
const DWELL_SAMPLE_INTERVAL_MS = 3_000;

// Long enough to guarantee the answer is still mid-stream when we switch
// chats, on a fast local model too.
const LONG_PROMPT =
  "List and describe, in detail, all 20 standard amino acids used in human " +
  "proteins — one full paragraph per amino acid, covering its side-chain " +
  "structure and its biological role. Do not use any tools; just answer " +
  "from what you know, and don't stop until you've covered all 20.";

interface ChatDetail {
  messages: Array<{ id: number; role: string; content: string }>;
}

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
async function sendPromptUntilStreaming(
  page: Page,
  prompt: string,
): Promise<void> {
  const composer = page.getByPlaceholder(/Message/);
  await composer.waitFor({ state: "visible", timeout: 15_000 });
  await composer.fill(prompt);
  await page.keyboard.press("Enter");
  await expect(
    page.getByTestId("chat-message-stream-caret"),
  ).toBeVisible({ timeout: STREAM_START_TIMEOUT_MS });
}

async function getChat(
  page: Page,
  backendURL: string,
  chatId: number,
): Promise<ChatDetail> {
  const resp = await page.request.get(`${backendURL}/api/chats/${String(chatId)}`);
  expect(
    resp.ok(),
    `GET /api/chats/${String(chatId)} → HTTP ${String(resp.status())}`,
  ).toBe(true);
  return (await resp.json()) as ChatDetail;
}

/** localStorage key lib/responseId.ts stores the chain anchor under. */
function responseIdKey(chatId: number): string {
  return `lmchat:sse:${String(chatId)}:rid`;
}

async function readLocalStorage(page: Page, key: string): Promise<string | null> {
  return page.evaluate((k: string) => localStorage.getItem(k), key);
}

test(
  "j13a: navigating to another chat mid-stream leaks no content/streaming state, and the origin chat's own state stays correct",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatIdA = await createChatViaRequest(page, backendURL, "J13a Switch A");
    const chatIdB = await createChatViaRequest(page, backendURL, "J13a Switch B");

    // First load of A is a real page.goto — establishes the SPA baseline the
    // same way every other dogfood journey does.
    await page.goto(`${backendURL}/chats/${String(chatIdA)}`);
    await waitForModelSelector(page);

    await sendPromptUntilStreaming(page, LONG_PROMPT);
    await expect(
      page.getByRole("button", { name: "Stop generation" }),
    ).toBeVisible({ timeout: 5_000 });
    await expect(page.locator('[data-message-role="assistant"]')).toHaveCount(1);

    // --- THE SWITCH — in-SPA navigation (pushState + popstate), NOT a full
    // reload. A full reload would remount everything and trivially "pass" by
    // destroying the very state this journey exists to exercise. ---
    await navigateToChatInSpa(page, chatIdB);

    // Dwell on B for real wall-clock time, sampling repeatedly — if A's
    // stream state were to leak in a few deltas late, a single instant
    // check right after navigation could miss it.
    for (let i = 0; i < DWELL_SAMPLE_COUNT; i++) {
      const anyMessageCount = await page.locator("[data-message-role]").count();
      const caretCount = await page
        .getByTestId("chat-message-stream-caret")
        .count();
      const stopVisible = await page
        .getByRole("button", { name: "Stop generation" })
        .isVisible()
        .catch(() => false);
      const sendBtnText = await page
        .getByTestId("composer-send-btn")
        .innerText()
        .catch(() => "");
      console.log(
        `J13a DIAG [on B, sample ${String(i)}]: messages=${String(anyMessageCount)} ` +
          `caret=${String(caretCount)} stopVisible=${String(stopVisible)} ` +
          `sendBtnText="${sendBtnText}"`,
      );
      expect(
        anyMessageCount,
        "chat B rendered a message while A's stream was still in flight — " +
          "the live stream leaked across the chat boundary",
      ).toBe(0);
      expect(
        caretCount,
        "chat B rendered A's streaming caret — cross-chat leak",
      ).toBe(0);
      expect(
        stopVisible,
        "chat B's composer shows a Stop control for a stream that isn't its own",
      ).toBe(false);
      expect(
        sendBtnText,
        "chat B's send button reads something other than 'Send' while B has " +
          "no in-flight stream of its own",
      ).toContain("Send");
      if (i < DWELL_SAMPLE_COUNT - 1) {
        await page.waitForTimeout(DWELL_SAMPLE_INTERVAL_MS);
      }
    }

    // --- BACK to A ---
    await navigateToChatInSpa(page, chatIdA);

    // A's own stream must still reach a clean terminal regardless of the
    // detour through B.
    await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send$/, {
      timeout: TURN_TIMEOUT_MS,
    });

    // GROUND TRUTH — A completed correctly (exactly 1 turn persisted).
    const afterA = await getChat(page, backendURL, chatIdA);
    expect(
      afterA.messages.map((m) => m.role),
      `expected [user, assistant] in A, got: ${JSON.stringify(afterA.messages.map((m) => m.role))}`,
    ).toEqual(["user", "assistant"]);
    expect(
      afterA.messages[1]?.content.length ?? 0,
      "A's assistant content is empty after the detour through B",
    ).toBeGreaterThan(0);

    // GROUND TRUTH — B was never touched by A's stream.
    const afterB = await getChat(page, backendURL, chatIdB);
    expect(
      afterB.messages.length,
      "chat B has persisted messages it was never sent — cross-chat write leak",
    ).toBe(0);

    // GROUND TRUTH — the response-id chain anchor landed on A only, never B.
    const ridA = await readLocalStorage(page, responseIdKey(chatIdA));
    const ridB = await readLocalStorage(page, responseIdKey(chatIdB));
    expect(
      ridA,
      "A's response_id was never stored — storeResponseId did not fire for " +
        "the chat that actually owned the completed stream",
    ).not.toBeNull();
    expect(
      ridB,
      "B has a response_id in localStorage despite never running a turn — " +
        "the completing A stream wrote its chain anchor under B's key " +
        "(the exact 'next turn continues the wrong conversation' bug)",
    ).toBeNull();

    assertNoConsoleErrors(collectErrors(), "j13a");
  },
);

test(
  "j13b: a message queued in the origin chat only drains once that chat is back on screen, never into whichever chat is showing",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(5_400_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatIdA = await createChatViaRequest(page, backendURL, "J13b Switch A");
    const chatIdB = await createChatViaRequest(page, backendURL, "J13b Switch B");
    const queuedTurnText = "In one sentence, name the capital of Japan.";

    await page.goto(`${backendURL}/chats/${String(chatIdA)}`);
    await waitForModelSelector(page);

    // Turn 1, mid-stream.
    await sendPromptUntilStreaming(page, LONG_PROMPT);
    await expect(
      page.getByRole("button", { name: "Stop generation" }),
    ).toBeVisible({ timeout: 5_000 });

    // Queue turn 2 while turn 1 is still streaming (mirrors j8).
    const composer = page.getByPlaceholder(/Message/);
    await composer.fill(queuedTurnText);
    await page.keyboard.press("Enter");
    const queueChip = page.getByTestId("composer-queue");
    await expect(queueChip).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("composer-queue-item").first()).toContainText(
      queuedTurnText,
    );

    // --- THE SWITCH — navigate to B with A's queue still holding one item. ---
    await navigateToChatInSpa(page, chatIdB);

    // B must show none of it: no messages, no queue chip (Composer's
    // `visibleQueue` filters by `item.chatId === chatId`, so A's queued item
    // must not render here), send button idle.
    expect(await page.locator("[data-message-role]").count()).toBe(0);
    expect(await page.getByTestId("composer-queue").count()).toBe(0);
    await expect(page.getByTestId("composer-send-btn")).toContainText("Send");

    // Wait for A's turn 1 to complete NATURALLY in the background — ground
    // truth via the persisted transcript (2 rows), not the DOM, since B is
    // the chat actually on screen right now.
    await expect
      .poll(
        async () => (await getChat(page, backendURL, chatIdA)).messages.length,
        { timeout: TURN_TIMEOUT_MS },
      )
      .toBeGreaterThanOrEqual(2);
    console.log("J13b DIAG: A's turn 1 completed while B was on screen");

    // THE CORE INVARIANT — with the owning chat (A) NOT on screen, the queue
    // must NOT auto-drain even though A's stream just went idle. Sample
    // across real wall-clock time: a bug here would fire turn 2 into
    // whichever chat happens to be open (B) the instant A's stream ends.
    for (let i = 0; i < 3; i++) {
      await page.waitForTimeout(2_000);
      const aCount = (await getChat(page, backendURL, chatIdA)).messages.length;
      const bCount = (await getChat(page, backendURL, chatIdB)).messages.length;
      console.log(
        `J13b DIAG [B on screen, sample ${String(i)}]: A.messages=${String(aCount)} ` +
          `B.messages=${String(bCount)}`,
      );
      expect(
        aCount,
        "A's queued turn 2 auto-sent while a DIFFERENT chat (B) was on " +
          "screen — the queue drained against the wrong chat",
      ).toBe(2);
      expect(
        bCount,
        "the queued message meant for A landed in B instead",
      ).toBe(0);
    }

    // --- BACK to A — NOW the queue-drain effect's precondition (owning
    // chat on screen AND not streaming) is satisfied; turn 2 must fire. ---
    await navigateToChatInSpa(page, chatIdA);

    await expect
      .poll(
        async () => (await getChat(page, backendURL, chatIdA)).messages.length,
        { timeout: TURN_TIMEOUT_MS },
      )
      .toBe(4);

    const finalA = await getChat(page, backendURL, chatIdA);
    expect(finalA.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(
      finalA.messages[2]?.content,
      "the queued message's text did not land as A's second user turn",
    ).toBe(queuedTurnText);
    expect(
      (finalA.messages[3]?.content ?? "").length,
      "turn 2's assistant content is empty",
    ).toBeGreaterThan(0);

    // B was NEVER touched by any of this.
    const finalB = await getChat(page, backendURL, chatIdB);
    expect(
      finalB.messages.length,
      "chat B ended up with persisted messages despite never being sent to",
    ).toBe(0);

    assertNoConsoleErrors(collectErrors(), "j13b");
  },
);
