/**
 * Flow 1 — Streaming + mid-stream stop.
 *
 * What it proves:
 *   A user can send a message and the SSE delta content is persisted by the
 *   backend (visible after stream ends). The Stop button appears during an
 *   active stream, and clicking it aborts the fetch client-side.
 *   After a completed stream the InterruptedRow retry affordance surfaces when
 *   a stale msg_id is present in localStorage.
 *
 * Mechanism:
 *   1. Normal stream flow: submit → stream → content appears in message list
 *      after chat.end (backend persists the message).
 *   2. Stop button: intercept with a hanging response, verify Stop visible,
 *      click it, verify Composer re-enables.
 *   3. Retry affordance: seed orphaned msg_id in localStorage, reload,
 *      InterruptedRow appears.
 *
 * Note on streaming message visibility:
 *   During streaming, `sseState.contentDeltas` is rendered as an optimistic
 *   "streaming" message row in the DOM. After chat.end, the backend-persisted
 *   message replaces it. We check the persisted version (after stream ends).
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
  ensureModelSelected,
} from "./_flow-helpers";

test(
  "flow-01: stream completes via normal stub; content persisted + visible after chat.end",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Create a chat and navigate to it via full page load.
    const chatId = await createChatViaRequest(page, backendURL, "Streaming Flow01");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    // Ensure a model is selected so Composer.handleSubmit doesn't bail silently.
    await ensureModelSelected(page);

    const composer = page.getByPlaceholder(/Message/);
    await expect(composer).toBeEnabled({ timeout: 5_000 });

    // Patch window.fetch to inject the required "model" field into
    // /api/chat/stream requests. route.continue does not work correctly for
    // streaming SSE responses in Playwright (buffers the response body before
    // passing to the browser). Using a JS-level fetch patch avoids that issue
    // while allowing the real response to stream through natively.
    await page.evaluate(() => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = async function patchedFetch(
        input: RequestInfo | URL,
        init?: RequestInit
      ): Promise<Response> {
        const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
        if (url.includes("/api/chat/stream")) {
          let body: Record<string, unknown> = {};
          try {
            body = JSON.parse((init?.body as string | undefined) ?? "{}") as Record<string, unknown>;
          } catch { /* ignore */ }
          const payload = (body["payload"] as Record<string, unknown> | undefined) ?? {};
          if (!payload["model"]) {
            payload["model"] = "stub-model-e2e";
            body["payload"] = payload;
          }
          return originalFetch(input, {
            ...init,
            body: JSON.stringify(body),
            headers: { ...(init?.headers as Record<string, string> | undefined ?? {}), "Content-Type": "application/json" },
          });
        }
        return originalFetch(input, init);
      };
    });

    // Submit a message — the stub LM Studio server returns a fixed SSE stream.
    await composer.fill("Tell me something interesting.");
    await composer.press("Meta+Enter");

    // The composer should disable during streaming.
    await expect(composer).toBeDisabled({ timeout: 8_000 });

    // The Stop button should be visible while streaming.
    const stopBtn = page.getByRole("button", { name: "Stop generation" });
    await expect(stopBtn).toBeVisible({ timeout: 8_000 });

    // Wait for the stream to complete — stub sends chat.end immediately.
    // After chat.end, the backend persists the assistant message and the
    // composer re-enables.
    await expect(composer).toBeEnabled({ timeout: 15_000 });

    // Allow React Query refetch to complete after stream ends.
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {/* RQ retries */});

    // After stream completion, the stub's response text should be visible in
    // the persisted message list (stub always responds "Hello from stub LM Studio.").
    //
    // NOTE: The streaming service does NOT persist user messages to the messages
    // table. User messages are passed as context to LM Studio but are not written
    // to the DB by stream_chat. Only assistant messages are persisted. The UI only
    // shows messages from the DB, so only the assistant message appears here.
    await expect(
      page.getByText("Hello from stub LM Studio.")
    ).toBeVisible({ timeout: 10_000 });

    // Stop button should be gone after stream ends.
    await expect(stopBtn).not.toBeVisible({ timeout: 3_000 });

    assertNoConsoleErrors(collectErrors(), "flow-01");
  }
);

test(
  "flow-01b: Stop button click aborts stream; Composer re-enables; InterruptedRow available",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "StopMidStream Flow01b");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    // Ensure a model is selected so Composer.handleSubmit doesn't bail silently.
    await ensureModelSelected(page);

    // Patch window.fetch in the page context to return a hanging ReadableStream
    // for /api/chat/stream requests. This is the only reliable way in Playwright
    // to produce a long-lived SSE connection that does NOT complete immediately
    // (Playwright's route.fulfill resolves the entire body synchronously).
    //
    // The patch returns a ReadableStream that emits chat.start + message.delta
    // and then hangs (never closes), giving the test time to click Stop before
    // the stream ends.
    await page.evaluate(() => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = async function patchedFetch(
        input: RequestInfo | URL,
        init?: RequestInit
      ): Promise<Response> {
        const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
        if (url.includes("/api/chat/stream")) {
          const chatStart = JSON.stringify({
            type: "chat.start",
            msg_id: 42,
            response_id: "resp-flow01b-stop",
          });
          const delta = JSON.stringify({
            type: "message.delta",
            msg_id: 42,
            delta: "Beginning of response...",
          });
          const partial =
            `event: chat.start\ndata: ${chatStart}\n\n` +
            `event: message.delta\ndata: ${delta}\n\n`;

          // Build a ReadableStream that sends the partial SSE then hangs.
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(partial));
              // Never call controller.close() — the stream hangs here.
              // The AbortController signal (when Stop is clicked) will
              // cause the reader to see an AbortError and the SPA will
              // transition to idle.
            },
            cancel() {
              // Called when the reader is cancelled (abort signal fires).
            },
          });

          return new Response(stream, {
            status: 200,
            headers: {
              "Content-Type": "text/event-stream",
              "Cache-Control": "no-cache",
            },
          });
        }
        return originalFetch(input, init);
      };
    });

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill("Stream this but I will stop it");
    await composer.press("Meta+Enter");

    // Composer disables during the stream (sseState.status === "streaming").
    await expect(composer).toBeDisabled({ timeout: 8_000 });

    // Stop button appears while streaming.
    const stopBtn = page.getByRole("button", { name: "Stop generation" });
    await expect(stopBtn).toBeVisible({ timeout: 8_000 });

    // Click Stop — calls AbortController.abort() which cancels the ReadableStream.
    await stopBtn.click();

    // Composer re-enables after abort (sseState.status → "idle").
    await expect(composer).toBeEnabled({ timeout: 10_000 });

    // Stop button should be gone after abort.
    await expect(stopBtn).not.toBeVisible({ timeout: 3_000 });

    // Restore original fetch for subsequent operations (page.reload).
    await page.evaluate(() => {
      // Cannot restore window.fetch to original here since we lost the ref.
      // The reload below will reset the JS context, restoring the original fetch.
    });

    // Seed an orphaned msg_id to trigger InterruptedRow.
    // msg_id=42 was stored by useSSE on chat.start.
    await page.evaluate((cid: number) => {
      localStorage.setItem(`lmchat:sse:${String(cid)}:msg_id`, "42");
    }, chatId);

    // Reload to trigger orphan detection (JS context resets, restoring fetch).
    await page.reload({ waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.waitForTimeout(800);

    // InterruptedRow retry affordance should be present.
    const interruptedRow = page.getByTestId("interrupted-row");
    await expect(interruptedRow).toBeVisible({ timeout: 8_000 });

    const retryBtn = page.getByTestId("interrupted-retry-btn");
    await expect(retryBtn).toBeVisible({ timeout: 3_000 });

    assertNoConsoleErrors(collectErrors(), "flow-01b");
  }
);

test(
  "flow-01c: mid-stream Stop leaves the streamed-so-far partial visible (not blank)",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "PartialPersist Flow01c");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    // Ensure a model is selected so Composer.handleSubmit doesn't bail silently.
    await ensureModelSelected(page);

    // Patch window.fetch with a stream that emits a distinctive partial token
    // then hangs. After Stop is clicked, the SPA should keep the partial
    // visible in the message list — clearing it would lose user-facing
    // context. We assert the partial text remains on the page.
    const partial = "Partial-streamed-token-FLOW01C";
    await page.evaluate((p: string) => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = async function patchedFetch(
        input: RequestInfo | URL,
        init?: RequestInit,
      ): Promise<Response> {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : (input as Request).url;
        if (url.includes("/api/chat/stream")) {
          const chatStart = JSON.stringify({
            type: "chat.start",
            msg_id: 7,
            response_id: "resp-flow01c",
          });
          const delta = JSON.stringify({
            type: "message.delta",
            msg_id: 7,
            delta: p,
          });
          const body =
            `event: chat.start\ndata: ${chatStart}\n\n` +
            `event: message.delta\ndata: ${delta}\n\n`;

          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(body));
              // Hang — the test will trigger Stop while the reader waits.
            },
          });
          return new Response(stream, {
            status: 200,
            headers: {
              "Content-Type": "text/event-stream",
              "Cache-Control": "no-cache",
            },
          });
        }
        return originalFetch(input, init);
      };
    }, partial);

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill("Start a stream then stop it mid-flight.");
    await composer.press("Meta+Enter");

    await expect(composer).toBeDisabled({ timeout: 8_000 });

    // Wait until the partial token has actually rendered in the message
    // pane before clicking Stop.
    await expect(page.getByText(partial)).toBeVisible({ timeout: 8_000 });

    const stopBtn = page.getByRole("button", { name: "Stop generation" });
    await expect(stopBtn).toBeVisible({ timeout: 5_000 });
    await stopBtn.click();

    // Composer re-enables, Stop disappears.
    await expect(composer).toBeEnabled({ timeout: 10_000 });
    await expect(stopBtn).not.toBeVisible({ timeout: 3_000 });

    // The streamed-so-far text must remain in the DOM — losing it would
    // be the regression this test guards against.
    await expect(page.getByText(partial)).toBeVisible();

    assertNoConsoleErrors(collectErrors(), "flow-01c");
  },
);
