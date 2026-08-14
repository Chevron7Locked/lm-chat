/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J3 — /research sub-session round-trip (streaming-3/4 + providers-2).
 *
 * Drives the slash-command sub-agent pipeline end-to-end against a REAL
 * model: open the Composer-local slash menu, launch /research, ask a
 * knowledge-answerable question in the clean-context sub-session, finalize a
 * summary, and inject it back into the main chat.
 *
 * Asserts the PIPELINE MECHANICS only — a message lands in the main thread
 * after the round-trip — never on specific model text or whether a tool was
 * called. Real models are nondeterministic; the pipeline's job is to move
 * the summary from the sub-session into the main chat, not to produce any
 * particular sentence.
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
// The finalize summary can trigger a long reasoning trace on a reasoning model
// (observed live: the model reasons extensively before emitting the summary),
// so allow a generous budget separate from a normal turn. This is a LOCAL-model
// harness: prompt processing + generation can legitimately run many minutes.
const FINALIZE_TIMEOUT_MS = 1_800_000;

test(
  "j3: /research sub-session summarizes and injects into the main chat",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatId = await createChatViaRequest(page, backendURL, "New Chat");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
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

    // Open the Composer-local slash menu by typing "/" as the first char
    // (SlashPalette.tsx is the DIFFERENT global Cmd/Ctrl+/ overlay — this is
    // Composer.tsx's inline autocomplete, SlashMenu.tsx, role=listbox
    // "Slash commands").
    const composer = page.getByPlaceholder(/Message/);
    await composer.click();
    await composer.fill("/");
    const slashMenu = page.getByRole("listbox", { name: "Slash commands" });
    await expect(slashMenu).toBeVisible({ timeout: 10_000 });

    // Select /research — Composer.handleSlashSelect dispatches immediately
    // (no inline args), which opens the sub-session panel and clears the
    // composer.
    await page.getByRole("option", { name: /^\/research/ }).click();
    // The sub-session panel is active once its "Cancel sub-session" control is
    // present ("Research mode" text appears in >1 place → strict-mode ambiguous).
    await expect(
      page.getByRole("button", { name: /Cancel sub-session/i }),
    ).toBeVisible({ timeout: 10_000 });

    // Ask a knowledge-answerable question — now routed into the sub-session
    // via maybeRouteSubmit (Chat.tsx / useSubSession.ts), not the main
    // stream. sendTurnAndWait's composer-re-enable wait works identically
    // here: Composer's `streaming` prop is bound to subSessionSSE's status
    // while a sub-session is active.
    await sendTurnAndWait(
      page,
      "In two sentences, what is a vector database?",
      TURN_TIMEOUT_MS,
    );

    // Belt-and-suspenders: the sub-session's completed answer renders with a
    // "Research" persona label (ChatMessage.tsx — suppressed while
    // streaming, so its presence confirms the turn actually finished).
    // Model-gated (only renders once the turn finalizes) — generous budget.
    await expect(
      page
        .locator('[data-testid="chat-message-persona-label"]', {
          hasText: "Research",
        })
        .first(),
    ).toBeVisible({ timeout: TURN_TIMEOUT_MS });

    // Finalize — streams a summary from the sub-session (a second real
    // model call).
    await page.getByRole("button", { name: "Summarize → main chat" }).click();
    await expect(page.getByText("Summary ready")).toBeVisible({
      timeout: FINALIZE_TIMEOUT_MS,
    });

    // Inject the summary into the main chat; this also cancels the
    // sub-session (handleSubSessionInject → cancelSubSession), so the main
    // message list re-renders in its place.
    await page
      .getByRole("button", { name: "Add to main chat →" })
      .click();

    // ASSERT: the summarized content lands as a NEW message in the MAIN
    // chat thread. While the sub-session is active the main message list
    // isn't rendered at all (Chat.tsx swaps to SubSessionPanel), so a
    // [data-message-role="assistant"] element appearing here can only come
    // from the post-inject main-chat render.
    await expect(
      page.locator('[data-message-role="assistant"]').first(),
    ).toBeVisible({ timeout: 30_000 });

    assertNoConsoleErrors(collectErrors(), "j3");
  },
);
