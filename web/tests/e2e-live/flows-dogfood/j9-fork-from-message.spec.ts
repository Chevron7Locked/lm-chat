/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J9 — fork a chat from an earlier assistant message (P1-dogfood).
 *
 * RED-ON-REVERT: this journey fails if 64b7ccb ("feat(chat): fork a chat
 * from any assistant message") is reverted. Before that commit there was
 * no per-message fork affordance. 64b7ccb adds the "Fork from here" action
 * (ChatMessage.tsx `canFork` + `chat-message-fork-btn-{id}`) on any
 * persisted, non-streaming assistant turn, wired through
 * `onForkFromHere` → `useChatCommands.handleForkFromMessage` →
 * `POST /api/chats/{id}/fork` (`at_message_id`, INCLUSIVE — copies rows
 * with `id <= at_message_id`, see chat_service.fork) → in-SPA
 * `navigate("/chats/{forked.id}")`.
 *
 * Drives 2 REAL turns against a real model (so there are 2 assistant
 * messages), forks from the FIRST (earlier) assistant message, and
 * asserts via the chat-detail API that the forked chat is a strict prefix
 * of the source — it ends at the fork point and does not carry turn 2
 * forward.
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
  sendTurnAndWait,
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 1_800_000;

interface ChatDetailMessages {
  messages: Array<{ id: number; role: string; content: string }>;
}

test(
  "j9: forking from an earlier assistant message creates a new chat truncated at that point",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatId = await createChatViaRequest(page, backendURL, "Fork Test");
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

    const sendBtn = page.getByTestId("composer-send-btn");

    // Turn 1 — sendTurnAndWait's own composer-enabled wait is a near no-op
    // (the composer is intentionally never disabled while streaming — see
    // j8/Composer.tsx), so wait for the REAL completion signal: the send
    // button settling back to "Send" once the turn is fully done.
    await sendTurnAndWait(
      page,
      "In one sentence, name the capital of France.",
      TURN_TIMEOUT_MS,
    );
    await expect(sendBtn).toHaveText(/Send$/, { timeout: TURN_TIMEOUT_MS });

    // Turn 2 — a second real turn so there are 2 assistant messages to
    // choose an EARLIER one from.
    await sendTurnAndWait(
      page,
      "In one sentence, name the capital of Japan.",
      TURN_TIMEOUT_MS,
    );
    await expect(sendBtn).toHaveText(/Send$/, { timeout: TURN_TIMEOUT_MS });

    // GROUND TRUTH — read back the persisted transcript to find the exact
    // message id to fork from (the FIRST, earlier assistant turn).
    const beforeResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`,
    );
    expect(beforeResp.ok()).toBeTruthy();
    const before = (await beforeResp.json()) as ChatDetailMessages;
    expect(
      before.messages.length,
      "expected 2 turns x (user+assistant) = 4 persisted rows, got: " +
        JSON.stringify(before.messages.map((m) => m.role)),
    ).toBe(4);
    expect(before.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    const earlierAssistant = before.messages[1]!;
    expect(earlierAssistant.role).toBe("assistant");

    // ACT — click "Fork from here" on the EARLIER assistant message.
    const forkBtn = page.getByTestId(
      `chat-message-fork-btn-${String(earlierAssistant.id)}`,
    );
    // The action bar is hover-revealed (chat.css: `.lmchat-message-row:hover
    // .lmchat-message-actions`), so hover the message row first — otherwise the
    // row itself intercepts the click, exactly as a mouse user would experience.
    await page
      .locator(`[data-message-id="${String(earlierAssistant.id)}"]`)
      .hover();
    await expect(forkBtn).toBeVisible({ timeout: 10_000 });
    await forkBtn.click();

    // ASSERT — in-SPA navigation to a NEW chat (handleForkFromMessage →
    // navigate(`/chats/${forked.id}`), never a full page load).
    // Wait for navigation to a DIFFERENT chat — the loose /chats/\d+ pattern
    // matches the CURRENT url and would resolve instantly with the old id,
    // before the fork's navigate() lands.
    await page.waitForURL(
      (url) => {
        const m = /\/chats\/(\d+)/.exec(url.pathname);
        return m !== null && Number(m[1]) !== chatId;
      },
      { timeout: 20_000 },
    );
    const match = /\/chats\/(\d+)/.exec(page.url());
    if (match === null) {
      throw new Error(`Cannot extract chatId from URL: ${page.url()}`);
    }
    const forkedChatId = Number(match[1]);
    expect(
      forkedChatId,
      "fork navigated back to the SAME chat instead of a new one",
    ).not.toBe(chatId);

    // The forked chat view is a real, live chat (composer ready).
    await page
      .getByPlaceholder(/Message/)
      .waitFor({ state: "visible", timeout: 10_000 });

    // GROUND TRUTH — the forked chat is a PREFIX of the source, ending at
    // (and including) the fork message, and carries NONE of turn 2.
    const afterResp = await page.request.get(
      `${backendURL}/api/chats/${String(forkedChatId)}`,
    );
    expect(afterResp.ok()).toBeTruthy();
    const after = (await afterResp.json()) as ChatDetailMessages;
    expect(
      after.messages.length,
      "forked chat must have FEWER rows than the source (truncated at the fork point)",
    ).toBeLessThan(before.messages.length);
    expect(
      after.messages.length,
      "forked chat should contain exactly turn 1's user+assistant pair",
    ).toBe(2);
    expect(after.messages[0]?.role).toBe("user");
    expect(after.messages[1]?.role).toBe("assistant");
    expect(
      after.messages[1]?.content,
      "the forked chat's terminal assistant message content doesn't match the fork point's content",
    ).toBe(earlierAssistant.content);

    // The SOURCE chat is untouched by the fork (still has all 4 rows).
    const sourceAfterResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`,
    );
    expect(sourceAfterResp.ok()).toBeTruthy();
    const sourceAfter = (await sourceAfterResp.json()) as ChatDetailMessages;
    expect(
      sourceAfter.messages.length,
      "the source chat's own transcript was mutated by forking — it must stay untouched",
    ).toBe(4);

    assertNoConsoleErrors(collectErrors(), "j9");
  },
);
