/**
 * Flow 56 — Stream 409 race: concurrent send rejected as stream_in_progress.
 *
 * What it proves (route-stubbed):
 *   When /api/chat/stream returns 409 with code="stream_in_progress", the FE
 *   surfaces the humanized error banner ("Another response is already
 *   streaming") and does NOT silently hang.
 *
 * Error path in the FE (useSSE.ts ~line 773-788):
 *   - A non-ok HTTP response is parsed: detail is extracted from the JSON body.
 *   - State transitions to { status: "error", error: { code: "http_409",
 *     message: <detail string> } }.
 *   - humanizeApiError first checks KNOWN["http_409"] — not present.
 *     Then http_<n> match finds 409, but STATUS_DEFAULTS has no 409 entry.
 *     Falls through to the message path: tryParseJsonEnvelope parses the
 *     JSON detail string, finds code="stream_in_progress", which IS in
 *     KNOWN → returns title "Another response is already streaming".
 *   - Chat.tsx renders <div data-testid="chat-stream-error"> with that title.
 *
 * Backend 409 shape (streaming.py):
 *   HTTP 409 body: { "detail": { "code": "stream_in_progress", "chat_id": 56 } }
 *   useSSE serialises body.detail → JSON.stringify → the message field.
 *
 * Simulate a 409 cleanly by making the SECOND stream POST return 409.
 * The first POST succeeds (stream is "in progress"); the second simulates
 * a second tab/browser already holding the stream lock.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ID = 56;

test.describe("Flow 56 — Stream 409 race", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults (probe hydration + correctly-typed
    // list/object endpoints, including a "qwen3" default model).
    await bootstrapAuthedApp(page);

    // Unified chats handler — trailing ** also matches the ?unscoped=true
    // query variant useChatsDirect issues.
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
              title: "409 race test",
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
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "409 race test",
            messages: [],
            has_more: false,
          }),
        });
      }
      return route.fallback();
    });
  });

  test("409 stream_in_progress surfaces error banner, not a silent hang", async ({
    page,
  }) => {
    // The stub returns 409 immediately (simulates: stream already locked by
    // another client/tab).  The FE makes exactly one POST per send; we reply
    // with 409 on the first (and only) request.
    await page.route("**/api/chat/stream", (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { code: "stream_in_progress", chat_id: CHAT_ID },
        }),
      });
    });

    await page.goto(`/chats/${String(CHAT_ID)}`);
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    // Send a message — the stub replies 409.
    await composer.fill("hello from a second tab");
    await page.getByLabel("Send message").click();

    // The error banner must appear.  data-testid="chat-stream-error" is
    // rendered by Chat.tsx when sseState.status === "error".
    const errorBanner = page.getByTestId("chat-stream-error");
    await expect(errorBanner).toBeVisible({ timeout: 8_000 });

    // humanizeApiError maps stream_in_progress → "Another response is already streaming".
    await expect(errorBanner).toContainText("Another response is already streaming");
  });

  test("after 409 banner is dismissed the composer is re-enabled", async ({
    page,
  }) => {
    await page.route("**/api/chat/stream", (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { code: "stream_in_progress", chat_id: CHAT_ID },
        }),
      });
    });

    await page.goto(`/chats/${String(CHAT_ID)}`);
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    await composer.fill("trigger the 409");
    await page.getByLabel("Send message").click();

    // Wait for error banner.
    await expect(page.getByTestId("chat-stream-error")).toBeVisible({
      timeout: 8_000,
    });

    // Dismiss the error — the "Dismiss" button calls stopStream() which
    // resets state to "idle" and re-enables the Composer.
    await page.getByTestId("chat-stream-error-dismiss").click();

    // Error banner gone.
    await expect(page.getByTestId("chat-stream-error")).not.toBeVisible({
      timeout: 4_000,
    });

    // Composer is usable again (not locked in "streaming" state).
    await expect(composer).toBeEnabled({ timeout: 4_000 });
  });
});
