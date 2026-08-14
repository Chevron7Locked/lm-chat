/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J7 — durable sub-session survives a reload (P1-P3 dogfood) + P4 reopen +
 * continue.
 *
 * The data-loss bug: a /research sub-session lived only in FE state + a live
 * SSE, so a reload lost the whole analysis. P2 persists it to the DB; P3 adds
 * restore-on-load. This journey drives a REAL /research turn against a real
 * model, RELOADS the page, and asserts:
 *   1. the sub-session + its transcript SURVIVE in the DB (the core fix) —
 *      GET /api/chats/{id}/sub-sessions returns it with content after reload;
 *   2. status='final' — the P4 dogfood-found bug (a completed turn's SSE
 *      response stays technically open a beat longer than a fast reload's
 *      disconnect watcher, mislabeling a COMPLETED session 'aborted'; fixed
 *      by marking the session final INSIDE the graceful finalize, not only
 *      in the outer teardown finally);
 *   3. reports what the FE does on reload (restore-on-load is live-only per D9);
 *   4. P4 REOPEN — since the finished session is NOT auto-restored (D9), the
 *      sub-session history affordance (⋯ overflow → "Sub-session history")
 *      finds it and reopening loads its full transcript back into the panel,
 *      for ANY status (this one is 'final', not live);
 *   5. P4 CONTINUE — sending a new turn from the reopened panel APPENDS onto
 *      the SAME sub_session_id (still exactly one row in the list; the
 *      transcript grows) instead of creating a second, disconnected row.
 *
 * Pipeline mechanics only — never asserts specific model text.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import {
  classifyFleet,
  configureLmStudio,
  sendTurnAndWait,
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 1_800_000;

test(
  "j7: a /research sub-session survives a page reload (persisted + fetchable)",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatId = await createChatViaRequest(page, backendURL, "Reload Test");
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

    // Launch /research + run one real turn.
    const composer = page.getByPlaceholder(/Message/);
    await composer.click();
    await composer.fill("/");
    await expect(
      page.getByRole("listbox", { name: "Slash commands" }),
    ).toBeVisible({ timeout: 10_000 });
    await page.getByRole("option", { name: /^\/research/ }).click();
    await expect(
      page.getByRole("button", { name: /Cancel sub-session/i }),
    ).toBeVisible({ timeout: 10_000 });
    await sendTurnAndWait(
      page,
      "In two sentences, what is a vector database?",
      TURN_TIMEOUT_MS,
    );
    // Turn finished when the Research persona label renders (suppressed mid-stream).
    // Model-gated — generous budget.
    await expect(
      page
        .locator('[data-testid="chat-message-persona-label"]', {
          hasText: "Research",
        })
        .first(),
    ).toBeVisible({ timeout: TURN_TIMEOUT_MS });

    // --- THE RELOAD ---
    await page.reload();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-testid="chat-header-model-select"]') !==
        null,
      null,
      { timeout: 30_000 },
    );
    await page.waitForTimeout(1500); // let restore-on-load settle

    // (1) THE FIX — the sub-session + transcript survived in the DB.
    const listResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}/sub-sessions`,
    );
    expect(listResp.ok()).toBeTruthy();
    const sessions = (await listResp.json()) as Array<{
      id: number;
      preset_id: string;
      status: string;
    }>;
    expect(
      sessions.length,
      "sub-session was NOT persisted — reload lost it (the bug)",
    ).toBeGreaterThan(0);
    const sid = sessions[0]!.id;

    const detailResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}/sub-sessions/${String(sid)}`,
    );
    expect(detailResp.ok()).toBeTruthy();
    const detail = (await detailResp.json()) as {
      status: string;
      messages: Array<{ role: string; content: string; state: string }>;
    };
    const assistant = detail.messages.find((m) => m.role === "assistant");
    expect(assistant, "no assistant row persisted for the sub-session").toBeTruthy();
    expect(
      (assistant?.content ?? "").length,
      "assistant content lost on reload",
    ).toBeGreaterThan(0);

    // DIAGNOSTIC — printed BEFORE the status assertion so the real terminal is
    // visible even when the assertion fails.
    console.log(
      `J7 DIAG: status=${detail.status} | ` +
        `msgs=[${detail.messages.map((m) => `${m.role}:${m.state}`).join(", ")}] | ` +
        `assistant.contentChars=${String((assistant?.content ?? "").length)}`,
    );

    // (2) P4 status fix — a turn that finished BEFORE the reload must read
    // status='final', not 'aborted'. The dogfood-found race: the SSE
    // response's underlying generator can still be tearing down (a few more
    // DB round trips past the graceful finalize) when a fast reload's
    // disconnect watcher fires; fixed by marking the session final INSIDE
    // the graceful finalize (_on_success) instead of only in the outer
    // teardown finally.
    expect(
      detail.status,
      "a turn that completed before the reload was mislabeled — expected " +
        "status='final', not 'aborted' (the P4 dogfood-found race)",
    ).toBe("final");

    // (3) Report the FE restore-on-load outcome (live-only per D9) — informational.
    const panelRestored = await page
      .getByRole("button", { name: /Cancel sub-session|Exit focus/i })
      .isVisible()
      .catch(() => false);
    console.log(
      `J7 RESULT: persisted=${String(sessions.length)} session(s); ` +
        `first status=${detail.status}; assistant state=${assistant?.state}; ` +
        `assistant content chars=${String((assistant?.content ?? "").length)}; ` +
        `FE auto-restored panel on reload=${String(panelRestored)}`,
    );

    // --- (4) P4 REOPEN — the finished session did NOT auto-restore (D9);
    // find it via the history affordance and reopen it. ---
    await page.getByTestId("topbar-overflow-trigger").click();
    await page.getByRole("menuitem", { name: "Sub-session history" }).click();
    const historyEntry = page.getByRole("listitem").first();
    await expect(historyEntry).toBeVisible({ timeout: 10_000 });
    await historyEntry.click();

    // Reopened: the panel shows with the ORIGINAL transcript, not a blank
    // fresh session — the Cancel button (any sub-session, live or reopened)
    // and the prior assistant content are both visible.
    await expect(
      page.getByRole("button", { name: /Cancel sub-session/i }),
    ).toBeVisible({ timeout: 10_000 });
    const priorAssistantSnippet = (assistant?.content ?? "").slice(0, 30);
    if (priorAssistantSnippet.length > 0) {
      await expect(page.getByText(priorAssistantSnippet)).toBeVisible({
        timeout: 10_000,
      });
    }

    // --- (5) P4 CONTINUE — a new turn from the reopened panel APPENDS onto
    // the SAME sub_session_id instead of creating a second row. ---
    await page
      .getByPlaceholder(/Message/)
      .fill("In one more sentence, name one popular vector database product.");
    await page.keyboard.press("Enter");
    // Anchor completion on the persisted transcript (the ground truth this step
    // verifies), NOT the DOM: the reopened sub-session panel renders the
    // continued turn through a different path, so a new [data-message-role] node
    // may not appear — but the backend appends the turn onto the same
    // sub_session_id. Wait until the 2nd assistant row is finalized.
    await expect
      .poll(
        async () => {
          const r = await page.request.get(
            `${backendURL}/api/chats/${String(chatId)}/sub-sessions/${String(sid)}`,
          );
          if (!r.ok()) return 0;
          const d = (await r.json()) as {
            messages: Array<{ role: string; state: string }>;
          };
          return d.messages.filter(
            (m) => m.role === "assistant" && m.state === "final",
          ).length;
        },
        { timeout: TURN_TIMEOUT_MS },
      )
      .toBeGreaterThanOrEqual(2);

    const listAfterContinueResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}/sub-sessions`,
    );
    expect(listAfterContinueResp.ok()).toBeTruthy();
    const sessionsAfterContinue = (await listAfterContinueResp.json()) as Array<{
      id: number;
      status: string;
    }>;
    expect(
      sessionsAfterContinue.length,
      "continuing a reopened session must APPEND onto the same row, not " +
        "create a second disconnected sub_sessions row",
    ).toBe(sessions.length);
    expect(sessionsAfterContinue[0]!.id).toBe(sid);
    expect(sessionsAfterContinue[0]!.status).toBe("final");

    const detailAfterContinueResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}/sub-sessions/${String(sid)}`,
    );
    expect(detailAfterContinueResp.ok()).toBeTruthy();
    const detailAfterContinue = (await detailAfterContinueResp.json()) as {
      status: string;
      messages: Array<{ role: string; content: string; state: string }>;
    };
    expect(
      detailAfterContinue.messages.length,
      "expected 2 turns x (user+assistant) = 4 rows under the SAME sub_session_id",
    ).toBe(4);
    const secondAssistant = detailAfterContinue.messages
      .filter((m) => m.role === "assistant")
      .at(-1);
    expect(secondAssistant, "no second assistant turn persisted").toBeTruthy();
    expect(
      (secondAssistant?.content ?? "").length,
      "continued turn's content lost",
    ).toBeGreaterThan(0);

    console.log(
      `J7 REOPEN+CONTINUE RESULT: sub_session_id stayed ${String(sid)}; ` +
        `sessions after continue=${String(sessionsAfterContinue.length)}; ` +
        `transcript rows after continue=${String(detailAfterContinue.messages.length)}; ` +
        `final status=${detailAfterContinue.status}`,
    );
  },
);
