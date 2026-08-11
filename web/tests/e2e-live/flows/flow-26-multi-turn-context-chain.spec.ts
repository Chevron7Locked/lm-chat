/**
 * Flow 26 — Multi-turn context chain: previous_response_id threading.
 *
 * What it proves:
 *   When a chat has a stored response_id in localStorage (keyed by chatId),
 *   the FE sends that id as `previous_response_id` on the next outgoing
 *   stream request, threading the multi-turn LM Studio context chain.
 *
 *   The FE stores the chain anchor in localStorage after each completed
 *   stream under the key `lmchat:sse:{chatId}:rid`. On submit, Chat.tsx
 *   calls `loadResponseId(cid)` and threads it into the payload:
 *     payload.previous_response_id = prevRid
 *
 *   This test:
 *     (a) Seeds 4 turns via API (2 user + 2 assistant) so the chat has
 *         visible history.
 *     (b) Seeds `localStorage['lmchat:sse:{chatId}:rid']` with a known
 *         response_id string (simulating what the FE would store after
 *         a real stream completed on turn 4).
 *     (c) Intercepts the turn-5 POST /api/chat/stream request.
 *     (d) Asserts:
 *         - `payload.previous_response_id` matches the seeded value.
 *         - The turn-5 input text (which contains a unique token referencing
 *           turn 1) is in the outgoing request's `input` blocks.
 *         - `chat_id` matches the created chat.
 *
 * Determinism: no real LM Studio needed. The stream intercept returns a
 * hanging ReadableStream so the test captures the request before any response.
 *
 * Why the FE reads from localStorage, not the DB:
 *   After each stream, Chat.tsx calls `storeResponseId(chatId, responseId)` —
 *   it puts the `chat.end` response_id from the SSE stream into localStorage.
 *   On the next submit, `loadResponseId(chatId)` reads it back. The DB
 *   `messages.metadata` field is not consulted by the FE for chain threading.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
  seedMessage,
} from "./_flow-helpers";

/** localStorage key Chat.tsx uses for the per-chat response_id chain anchor. */
function ridKey(chatId: number): string {
  return `lmchat:sse:${String(chatId)}:rid`;
}

test(
  "flow-26a: turn-5 stream request carries previous_response_id seeded in localStorage",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Create a chat.
    const chatId = await createChatViaRequest(page, backendURL, "Flow26 Chain Test");

    // Seed 4 turns so the chat looks like a real multi-turn conversation.
    // Turn 1 user carries a unique token to confirm it's in the input on turn 5.
    const TURN1_TOKEN = `FLOW26_T1_${Date.now().toString()}`;
    await seedMessage(page, backendURL, chatId, "user", TURN1_TOKEN);
    await seedMessage(page, backendURL, chatId, "assistant", "Turn 2 answer.");
    await seedMessage(page, backendURL, chatId, "user", "Turn 3 question.");
    await seedMessage(page, backendURL, chatId, "assistant", "Turn 4 answer.");

    // Navigate to the chat so the FE's JS context is initialised.
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    // Seed the localStorage chain anchor that the FE would have stored after
    // turn 4's stream completed.
    const SEEDED_RID = "resp-flow26-turn4-seeded";
    await page.evaluate(
      ({ key, rid }: { key: string; rid: string }) => {
        localStorage.setItem(key, rid);
      },
      { key: ridKey(chatId), rid: SEEDED_RID }
    );

    // Wait for model dropdown to be populated.
    const modelSelect = page.locator('[data-testid="chat-header-model-select"]');
    await modelSelect.waitFor({ state: "visible", timeout: 5_000 });
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]'
        );
        return sel !== null && Array.from(sel.options).some((o) => o.value !== "");
      },
      null,
      { timeout: 30_000 }
    );
    if ((await modelSelect.inputValue()) === "") {
      await modelSelect.selectOption({ index: 1 });
    }

    // Allow message list to settle.
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => { /* ok */ });

    // Intercept the outgoing stream request body before it reaches the BE.
    // We return a hanging stream so the FE doesn't error out immediately.
    const capturedBodyPromise = page.evaluate((): Promise<string> => {
      return new Promise<string>((resolve, reject) => {
        const originalFetch = window.fetch.bind(window);
        window.fetch = async function patchedFetch(
          input: RequestInfo | URL,
          init?: RequestInit
        ): Promise<Response> {
          const url =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : (input as Request).url;
          if (url.includes("/api/chat/stream")) {
            resolve((init?.body as string | undefined) ?? "");
            const stream = new ReadableStream<Uint8Array>({
              start(_controller) { /* hang — avoids FE fetch rejection */ },
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
        setTimeout(() => reject(new Error("fetch patch timed out after 20s")), 20_000);
      });
    });

    // Submit turn 5 — this triggers the intercepted fetch.
    const composer = page.getByPlaceholder(/Message/);
    await composer.fill(`Turn 5 referencing earlier context: ${TURN1_TOKEN}`);
    await composer.press("Meta+Enter");

    // Await the captured body.
    const capturedBody = await capturedBodyPromise;

    // Parse.
    let parsedBody: {
      chat_id?: number;
      payload?: {
        previous_response_id?: string | null;
        input?: Array<{ type: string; content?: string }>;
        model?: string;
      };
    };
    try {
      parsedBody = JSON.parse(capturedBody) as typeof parsedBody;
    } catch {
      throw new Error(`Could not parse captured stream body: ${capturedBody.slice(0, 200)}`);
    }

    // Assertion (a): previous_response_id matches the localStorage-seeded value.
    expect(parsedBody.payload?.previous_response_id).toBe(SEEDED_RID);

    // Assertion (b): turn-5 user text (containing TURN1_TOKEN) is in input blocks.
    const inputTexts = (parsedBody.payload?.input ?? [])
      .filter((b) => b.type === "text")
      .map((b) => b.content ?? "")
      .join(" ");
    expect(inputTexts).toContain(TURN1_TOKEN);

    // Assertion (c): chat_id is threaded.
    expect(parsedBody.chat_id).toBe(chatId);

    assertNoConsoleErrors(collectErrors(), "flow-26a");
  }
);

test(
  "flow-26b: first-turn stream request has no previous_response_id when localStorage is empty",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "Flow26b First Turn");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    // Ensure no stale rid in localStorage.
    await page.evaluate((key: string) => {
      localStorage.removeItem(key);
    }, ridKey(chatId));

    const modelSelect = page.locator('[data-testid="chat-header-model-select"]');
    await modelSelect.waitFor({ state: "visible", timeout: 5_000 });
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]'
        );
        return sel !== null && Array.from(sel.options).some((o) => o.value !== "");
      },
      null,
      { timeout: 30_000 }
    );
    if ((await modelSelect.inputValue()) === "") {
      await modelSelect.selectOption({ index: 1 });
    }

    // Capture the outgoing stream body.
    const capturedBodyPromise = page.evaluate((): Promise<string> => {
      return new Promise<string>((resolve, reject) => {
        const originalFetch = window.fetch.bind(window);
        window.fetch = async function patchedFetch(
          input: RequestInfo | URL,
          init?: RequestInit
        ): Promise<Response> {
          const url =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : (input as Request).url;
          if (url.includes("/api/chat/stream")) {
            resolve((init?.body as string | undefined) ?? "");
            const stream = new ReadableStream<Uint8Array>({
              start(_controller) { /* hang */ },
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
        setTimeout(() => reject(new Error("fetch patch timed out")), 20_000);
      });
    });

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill("Hello, this is the first turn.");
    await composer.press("Meta+Enter");

    const capturedBody = await capturedBodyPromise;

    let parsedBody: { payload?: { previous_response_id?: string | null } };
    try {
      parsedBody = JSON.parse(capturedBody) as typeof parsedBody;
    } catch {
      throw new Error(`Could not parse captured stream body: ${capturedBody.slice(0, 200)}`);
    }

    // A fresh chat with no stored rid must send null or omit previous_response_id.
    const prId = parsedBody.payload?.previous_response_id;
    expect(prId === null || prId === undefined).toBe(true);

    assertNoConsoleErrors(collectErrors(), "flow-26b");
  }
);

test(
  "flow-26c: after a stream completes, localStorage stores the response_id for the next turn",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "Flow26c RID Persist");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    // Ensure clean state.
    await page.evaluate((key: string) => { localStorage.removeItem(key); }, ridKey(chatId));

    const modelSelect = page.locator('[data-testid="chat-header-model-select"]');
    await modelSelect.waitFor({ state: "visible", timeout: 5_000 });
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]'
        );
        return sel !== null && Array.from(sel.options).some((o) => o.value !== "");
      },
      null,
      { timeout: 30_000 }
    );
    if ((await modelSelect.inputValue()) === "") {
      await modelSelect.selectOption({ index: 1 });
    }

    // Patch fetch to return a normal SSE stream with a known response_id.
    // The stub's buildSseFixture uses "stub-resp-1" — the FE should store that.
    // We let the real stream proxy through to the backend, which proxies to the
    // stub server. The stub always responds with response_id "stub-resp-1".
    await page.evaluate((burl: string) => {
      const originalFetch = window.fetch.bind(window);
      window.fetch = async function patchedFetch(
        input: RequestInfo | URL,
        init?: RequestInit
      ): Promise<Response> {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : (input as Request).url;
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
            headers: {
              ...(init?.headers as Record<string, string> | undefined ?? {}),
              "Content-Type": "application/json",
            },
          });
        }
        return originalFetch(input, init);
      };
    }, backendURL);

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill("First message — let the stream complete.");
    await composer.press("Meta+Enter");

    // Wait for stream to complete (composer re-enables).
    await expect(composer).toBeEnabled({ timeout: 15_000 });
    await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => { /* ok */ });

    // After stream completion, localStorage should contain the response_id
    // emitted by the stub's chat.end event ("stub-resp-1").
    const storedRid = await page.evaluate((key: string): string | null => {
      return localStorage.getItem(key);
    }, ridKey(chatId));

    // The stub always sends response_id = "stub-resp-1" in chat.start and chat.end.
    // Verify the FE stored it.
    expect(storedRid).toBe("stub-resp-1");

    assertNoConsoleErrors(collectErrors(), "flow-26c");
  }
);
