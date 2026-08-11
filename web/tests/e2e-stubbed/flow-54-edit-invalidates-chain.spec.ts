/**
 * Flow 54 — Edit user message invalidates chain (no stale previous_response_id).
 *
 * What it proves (route-stubbed):
 *   1. Send a message; the stub returns a response_id ("rid-turn-1") in
 *      chat.start.  The FE stores this in localStorage
 *      (lmchat:sse:<chatId>:rid) and would forward it as
 *      previous_response_id on the NEXT turn.
 *   2. The user edits the just-sent user message (chat-message-edit-btn-*
 *      → edit textarea → save).  The PATCH /api/messages/:id fires.
 *   3. The user sends a new message.  The intercepted /api/chat/stream
 *      POST body MUST NOT contain previous_response_id, confirming the
 *      chain was invalidated by the edit.
 *
 * Design decision (2026-06-12 remediation plan §3):
 *   "invalidate chain on every edit" — editing any user message must
 *   clear the stored response_id so the next turn starts a fresh chain
 *   rather than anchoring to a context that no longer reflects the
 *   edited history.
 *
 * Mechanism under test:
 *   - handleEditUserMessage (Chat.tsx) → editMessage.mutateAsync → clears rid.
 *   - Subsequent handleSubmit → loadResponseId() → null → no
 *     previous_response_id on the enrichedPayload sent to useSSE.start().
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ID = 54;

/** Minimal SSE body with a response_id so the FE stores it. */
function buildSseWithRid(rid: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ type: "chat.start", msg_id: 1, response_id: rid })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ type: "message.delta", delta: "Turn 1 reply." })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ type: "chat.end", stop_reason: "stop" })}\n\n`
  );
}

/** Minimal SSE body without response_id (LM Studio-style). */
function buildSseNoRid(): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ type: "chat.start", msg_id: 2 })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ type: "message.delta", delta: "Turn 2 reply." })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ type: "chat.end", stop_reason: "stop" })}\n\n`
  );
}

test.describe("Flow 54 — Edit invalidates response_id chain", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults (probe hydration + correctly-typed
    // list/object endpoints, including a "qwen3" default model).
    await bootstrapAuthedApp(page);
  });

  test("edit user message clears response_id so next stream has no previous_response_id", async ({
    page,
  }) => {
    const capturedRids: (string | undefined)[] = [];
    let streamCallCount = 0;

    // The user message that was sent on turn 1 — after refetch it will appear
    // with this id so the edit button is reachable.
    const USER_MSG_ID = 541;
    const ASST_MSG_ID = 542;

    // Stream handler — captures payload.previous_response_id for each call.
    await page.route("**/api/chat/stream", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const raw = route.request().postData() ?? "{}";
      const body = JSON.parse(raw) as {
        chat_id?: number;
        payload?: { previous_response_id?: string };
      };
      capturedRids.push(body.payload?.previous_response_id);
      streamCallCount++;

      if (streamCallCount === 1) {
        // Turn 1 — return a response_id so the FE stores it.
        // Advance messagesVersion NOW so that when the FE calls
        // refetchMessages() on stream-complete, the chat detail stub
        // returns the user+assistant pair.  The baseline was set to 0
        // (no messages on initial load), so count=2 > baseline=0 will
        // clear pendingUser and reveal the real persisted messages with
        // their edit buttons.
        messagesVersion = 1;
        return route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: buildSseWithRid("rid-turn-1"),
        });
      }
      // Turn 2 (after edit) — no response_id required from the stub.
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSseNoRid(),
      });
    });

    // Edit PATCH — succeed so the UI doesn't roll back.
    await page.route(`**/api/messages/${String(USER_MSG_ID)}`, (route) => {
      if (route.request().method() !== "PATCH") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: USER_MSG_ID,
          chat_id: CHAT_ID,
          role: "user",
          content: "edited user message",
          reasoning_content: null,
          created_at: "2026-06-01T12:00:01Z",
          state: "final",
          response_id: null,
          model_id: null,
        }),
      });
    });

    // Messages are EMBEDDED in GET /api/chats/:id (no separate /messages
    // route).  Version the embedded list: empty before turn 1 completes, then
    // user+assistant so the edit button renders.
    let messagesVersion = 0;
    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const path = new URL(route.request().url()).pathname;
      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: CHAT_ID,
              user_id: 1,
              title: "Chain invalidation test",
              folder: null,
              pinned: false,
              created_at: "2026-06-01T12:00:00Z",
              updated_at: "2026-06-01T12:00:00Z",
              settings: {},
              display_order: 0,
              incognito: false,
              incognito_expires_at: null,
              model_id: "qwen3",
            },
          ]),
        });
      }
      if (method === "GET" && path === `/api/chats/${String(CHAT_ID)}`) {
        const messages =
          messagesVersion === 0
            ? []
            : [
                {
                  id: USER_MSG_ID,
                  chat_id: CHAT_ID,
                  role: "user",
                  content: "original user message",
                  reasoning_content: null,
                  created_at: "2026-06-01T12:00:01Z",
                  state: "final",
                  response_id: null,
                  model_id: null,
                },
                {
                  id: ASST_MSG_ID,
                  chat_id: CHAT_ID,
                  role: "assistant",
                  content: "Turn 1 reply.",
                  reasoning_content: null,
                  created_at: "2026-06-01T12:00:02Z",
                  state: "final",
                  response_id: "rid-turn-1",
                  model_id: null,
                },
              ];
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "Chain invalidation test",
            messages,
            has_more: false,
          }),
        });
      }
      return route.fallback();
    });

    // ── Navigate + Turn 1 ─────────────────────────────────────────────────
    await page.goto(`/chats/${String(CHAT_ID)}`);
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    await composer.fill("original user message");
    await page.getByRole("button", { name: "Send message", exact: true }).click();

    // Wait for the Composer to re-enable (stream complete + refetch done).
    await expect(composer).toBeEnabled({ timeout: 8_000 });

    // Turn 1 should NOT carry previous_response_id (nothing stored yet).
    expect(capturedRids[0]).toBeUndefined();

    // ── Edit user message ─────────────────────────────────────────────────
    // The action bar is CSS-hover-revealed; hover the message row first so
    // the parent element no longer intercepts pointer events on the button.
    const msgRow = page.locator(`[data-message-id="${String(USER_MSG_ID)}"]`);
    await msgRow.hover();
    const editBtn = page.getByTestId(`chat-message-edit-btn-${String(USER_MSG_ID)}`);
    await expect(editBtn).toBeVisible({ timeout: 6_000 });
    await editBtn.click();

    const editTextarea = page.getByTestId(
      `chat-message-edit-textarea-${String(USER_MSG_ID)}`
    );
    await expect(editTextarea).toBeVisible({ timeout: 4_000 });
    await editTextarea.fill("edited user message");
    await page.getByTestId(`chat-message-edit-save-${String(USER_MSG_ID)}`).click();

    // Wait for PATCH to complete (edit button re-appears / textarea gone).
    await expect(editTextarea).not.toBeVisible({ timeout: 4_000 });

    // ── Turn 2 — after edit ───────────────────────────────────────────────
    await composer.fill("follow-up after edit");
    await page.getByRole("button", { name: "Send message", exact: true }).click();

    await expect.poll(() => streamCallCount >= 2, { timeout: 8_000 }).toBe(true);

    // The second stream request MUST NOT carry the stale previous_response_id.
    // Chain was invalidated by the edit.
    expect(capturedRids[1]).toBeUndefined();
  });
});
